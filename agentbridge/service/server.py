# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""mcp-agentbridge: MCP tools to run a per-user fleet of Claude agents over
RabbitMQ. Every tool resolves the caller's tenant from the reverse proxy's user
header first and acts only inside it.

Logging records user, tool, worker ids, byte counts and exit codes — never task
text, agent output, secrets, or a bus URL."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders

from .. import expect as expect_mod
from .. import ledger
from ..bridge import validate_task_options
from ..bus import fold_messages
from . import (
    backends,
    broker,
    budget,
    checks,
    config,
    fleet,
    gitconfig,
    gitcred,
    ratelimit,
    selfservice,
    ui,
    workers,
    workspace,
)
from . import (
    checkpoint as checkpoint_mod,
)
from .roles import ROLES
from .tenant import (
    TenantError,
    pick_credential,
    podman_secret_names,
    tenant_from_headers,
)
from .workspace import WorkspaceError

log = logging.getLogger("mcp-agentbridge")

INSTRUCTIONS = """\
You are the orchestrator of your own fleet of Claude agents running on this
service. Agents are stateless and disposable: each task runs in a fresh
container, so put ALL the context an agent needs into every task. Typical flow:
spawn_worker -> send_task -> collect (repeat collect until each task reports
done) -> retire_worker. Respond to idle ("waiting") workers with a next task or
retire them. Implementers commit signed-off on a branch in the workspace and
never push. Escalate destructive or out-of-scope actions to the human before
asking an agent to perform them. You are the curator: keep what is worth
keeping (workspace_write, your own notes) and let the rest go.

Verify with run_check; do not ask an agent whether the tests passed. Loop
implement -> run_check -> fix, with an explicit stop condition (at most three
rounds), then escalate to the human rather than looping again.

rollback_workspace undoes a task's commits and is destructive: ask the human
first, every time, and tell them which task and repository you mean."""

mcp = FastMCP("agentbridge", instructions=INSTRUCTIONS)

# Dependency sentinel (B008: must not call it in default arg position)
_current_headers = CurrentHeaders()

# The live fleet page: read-only routes on this same app and port.
ui.register(mcp)


def _fail(error: Exception | str, hint: str | None = None) -> dict:
    out = {"success": False, "error": str(error)}
    if hint:
        # A separate key, not an appended string: the caller reads the error to
        # see what git said and the diagnosis to see what it means.
        out["diagnosis"] = hint
    return out


async def _credential(tenant):
    return pick_credential(tenant, await podman_secret_names())


async def _worker_ids(tenant) -> set[str]:
    """Worker ids only: a check shares the unit prefix but takes no tasks."""
    return {w["id"] for w in await workers.list_units(tenant)
            if w.get("kind") != "check"}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def whoami(headers: dict = _current_headers) -> dict:
    """Show your tenant: user, bus vhost, workspace, whether a worker
    credential is registered, worker limit and current workers."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    credential = await _credential(tenant)
    log.info("user=%s tool=whoami", tenant.name)
    return {
        "user": tenant.name,
        "vhost": tenant.vhost,
        "workspace": str(tenant.workspace),
        "credential_registered": credential is not None,
        "credential_kind": credential[1] if credential else None,
        # So a caller can tell why an implementer cannot commit, without
        # having to guess (set it with set_git_identity).
        "git_identity": gitconfig.read_identity(tenant),
        "max_workers": tenant.max_workers,
        # Which backends this tenant may spawn on, so the question does not
        # need a second call. `anthropic` appears only with a credential
        # registered, because without one it cannot in fact be used.
        "backends_available": sorted(
            name for name, b in backends.for_tenant(tenant.name).items()
            if b.kind != "anthropic" or credential is not None),
        "workers": await workers.list_units(tenant),
        # What this week cost, so the question "am I spending anything?" does
        # not need a second tool call.
        "usage_7d": await asyncio.to_thread(_usage_7d, tenant),
        # Zero limits mean unlimited; `enforced` says whether anything bites.
        "budget": await asyncio.to_thread(budget.status, tenant),
    }


def _usage_7d(tenant) -> dict:
    recent = ledger.records(tenant.ledger, limit=ledger.MAX_RECORDS, since="7d")
    summary = ledger.summarize(recent, by="model")
    return {
        "tasks": summary["totals"]["tasks"],
        "cost_usd": summary["totals"]["cost_usd"],
        "cost_estimated": summary["cost_estimated"],
        # The ledger keeps a bounded window, so say when the answer is a floor
        # rather than a total.
        "truncated": len(recent) >= ledger.MAX_RECORDS,
    }


async def _unit_present(tenant, check_id: str) -> bool:
    name = checks.unit_name(tenant, check_id)
    for unit in await workers.list_units(tenant):
        if unit["unit"] == name:
            return unit.get("active") in ("active", "activating")
    return False


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": False})
async def run_check(
    command: list[str],
    role: str = "reviewer",
    workdir: str = ".",
    timeout: int = 300,
    wait: float = 60,
    headers: dict = _current_headers,
) -> dict:
    """Run a command against your workspace and report its exit code.

    **Use this instead of asking an agent whether the tests passed.** An agent
    reporting on its own work is the least reliable witness available. This
    runs a process and gives you `rc`.

    The container has no credential, no claude binary and `--network=none`, so
    it cannot reach a model, the broker or a package index — dependencies must
    be in the role's image. Your workspace is mounted as an overlay: the
    command may write freely, and every write is discarded, so a check can
    never change what it is checking.

    `command` is an argv list, e.g. `["pytest", "-q"]`; `["sh", "-c", "a && b"]`
    is fine, since that shell runs inside the sandbox. It waits up to `wait`
    seconds and otherwise returns `state: "running"` — poll `check_result`.
    A check counts against your worker limit while it runs.
    """
    try:
        tenant = tenant_from_headers(headers)
        checks.validate_command(command)
        timeout = checks.validate_timeout(timeout)
    except (TenantError, checks.CheckError) as e:
        return _fail(e)
    if role not in ROLES:
        return _fail(f"unknown role {role!r}; available: {', '.join(ROLES)}")

    existing = await workers.list_units(tenant)
    active = [u for u in existing if u["active"] in ("active", "activating")]
    if len(active) + 1 > tenant.max_workers:
        return _fail(
            f"worker limit reached: {len(active)} running (workers and checks "
            f"share the limit of {tenant.max_workers}); retire something first"
        )

    check_id = checks.new_check_id()
    try:
        argv = await asyncio.to_thread(
            checks.build_command, tenant, role, check_id, command, workdir, timeout)
    except (checks.CheckError, WorkspaceError) as e:
        return _fail(e)

    checks.checks_dir(tenant).mkdir(parents=True, exist_ok=True)
    try:
        await workers.start(argv)
    except RuntimeError as e:
        return _fail(f"could not start the check: {e}")
    log.info("user=%s tool=run_check check=%s role=%s timeout=%d",
             tenant.name, check_id, role, timeout)

    result = await checks.wait_for(
        tenant, check_id,
        min(float(wait), config.MAX_COLLECT_SECONDS),
        lambda: _unit_present(tenant, check_id),
    )
    log.info("user=%s tool=run_check check=%s state=%s rc=%s duration=%s",
             tenant.name, check_id, result["state"], result["rc"],
             result.get("duration_s"))
    return result


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def check_result(
    check_id: str,
    wait: float = 60,
    tail_bytes: int = checks.TAIL_BYTES,
    headers: dict = _current_headers,
) -> dict:
    """Poll a check started by `run_check`.

    `state` is `running`, `done`, or `timeout` when the check's unit vanished
    without recording a result. `rc` of 0 means the command succeeded; `tail`
    is the end of its combined output, and `log_bytes` says how much there is.
    """
    try:
        tenant = tenant_from_headers(headers)
        checks.validate_check_id(check_id)
    except (TenantError, checks.CheckError) as e:
        return _fail(e)
    try:
        result = await checks.wait_for(
            tenant, check_id,
            min(float(wait), config.MAX_COLLECT_SECONDS),
            lambda: _unit_present(tenant, check_id),
        )
        if tail_bytes != checks.TAIL_BYTES:
            result.update(await asyncio.to_thread(
                checks.read_tail, tenant, check_id, tail_bytes))
    except checks.CheckError as e:
        return _fail(e)
    log.info("user=%s tool=check_result check=%s state=%s rc=%s",
             tenant.name, check_id, result["state"], result["rc"])
    return result


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "openWorldHint": False})
async def rollback_workspace(
    task_id: str,
    repo: str | None = None,
    force: bool = False,
    headers: dict = _current_headers,
) -> dict:
    """Undo a task's committed work, using the checkpoint taken before it ran.

    **Destructive, and requires the human's approval first** — it discards
    commits and tracked changes made since the checkpoint. Get that approval
    before calling it, as the approval policy requires for anything
    destructive.

    Resets each repository's recorded branch to where it was before `task_id`
    ran. It never runs `git clean`, so untracked files survive, and it records
    the discarded tip as `refs/agentbridge/post/<task_id>` so the rollback
    itself can be undone. A repository that was already dirty when the
    checkpoint was taken is refused unless `force=True`: those changes were
    never captured, so resetting would destroy work no checkpoint holds.

    `repo` limits it to one repository (as the checkpoint reported it);
    omitted, every repository with a checkpoint for that task is rolled back.
    Only tasks on read-write roles have checkpoints.

    Read the result rather than assuming success: `rolled_back` lists
    repositories whose working tree was moved, `branch_only` those where HEAD
    had since moved to another branch so only the recorded branch was reset
    (normal when an implementer commits on its own branch — the work is still
    in the tree, on that branch), `refused` those that were dirty at
    checkpoint time, and `no_checkpoint` those with nothing recorded for this
    task.
    """
    try:
        tenant = tenant_from_headers(headers)
        checkpoint_mod.validate_task_id(task_id)
        target = checkpoint_mod.validate_repo(repo)
        ratelimit.check(tenant.name, "rollback_workspace")
    except (TenantError, checkpoint_mod.CheckpointError,
            ratelimit.RateLimited) as e:
        return _fail(e)

    try:
        role = checkpoint_mod.role_for_worker("implementer")
        argv = checkpoint_mod.rollback_command(
            tenant, role, task_id, target, force)
        result = await checkpoint_mod.run_leaf(argv)
    except checkpoint_mod.CheckpointError as e:
        return _fail(e)

    repos = result.get("repos") or []
    if not repos:
        return _fail(
            f"no repository found in your workspace to roll back (looked for "
            f"a checkpoint from task {task_id!r})"
        )

    def by_status(*wanted):
        return [r["repo"] for r in repos if r.get("status") in wanted]

    # Reported apart, because they are different outcomes and lumping them
    # together would claim work was undone when it was not.
    reset = by_status("reset", "reset_detached")
    branch_only = by_status("branch_reset")
    refused = by_status("refused_dirty")
    missing = by_status("no_checkpoint")

    response = {
        "task_id": task_id,
        "repos": repos,
        "rolled_back": reset,
        "branch_only": branch_only,
        "refused": refused,
        "no_checkpoint": missing,
        "untracked_files_kept": True,
    }
    if branch_only:
        response["note"] = (
            "HEAD is on a different branch than the checkpoint recorded, so "
            "only the recorded branch was moved and the working tree still "
            "holds the task's work. This is normal when an implementer commits "
            "on its own branch. Check out the recorded branch to see the "
            "rolled-back state."
        )
    if not reset and not branch_only:
        response["note"] = (
            f"nothing was rolled back: no repository had a checkpoint for task "
            f"{task_id!r}. Only tasks on read-write roles are checkpointed, "
            f"and an agent can delete its own refs."
            if missing and not refused else
            "nothing was rolled back; see each repository's status."
        )
    log.info("user=%s tool=rollback_workspace task=%s reset=%d branch=%d "
             "refused=%d none=%d force=%s", tenant.name, task_id, len(reset),
             len(branch_only), len(refused), len(missing), force)
    return response


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def fleet_status(headers: dict = _current_headers) -> dict:
    """What your whole fleet is doing right now, in one call.

    Per worker: state (`starting`, `idle`, `working`, `retiring`, or `gone` /
    `failed` from systemd), the task it is on and since when, the tool it last
    called, how many tasks it has finished and the last exit code. Plus queue
    depths, counts by state and your 7-day usage.

    Metadata only and small, so it is cheap to call before deciding what to do
    next — prefer it to `list_workers` when you want to know whether anyone is
    busy. `gone` or `failed` means a worker died; retire it and spawn a fresh
    one.
    """
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    units = await workers.list_units(tenant)
    result = await fleet.snapshot(tenant, units)
    log.info("user=%s tool=fleet_status workers=%d",
             tenant.name, result["totals"]["workers"])
    return result


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def task_history(
    limit: int = 50,
    since: str | None = None,
    role: str | None = None,
    headers: dict = _current_headers,
) -> dict:
    """Your most recent finished tasks, newest first.

    One record per task: ids, worker, role, model, effort, exit code, duration,
    turns, token counts and cost. It holds **metadata only** — never task text,
    agent output or tool inputs — and it is written whether or not anyone
    called `collect`, so it is the reliable answer to "what did my fleet do?".

    `since` takes 7d, 24h, 30m, a date (2026-10-28) or an ISO timestamp.
    """
    try:
        tenant = tenant_from_headers(headers)
        rows = await asyncio.to_thread(
            ledger.records, tenant.ledger, limit, since, role)
    except (TenantError, ValueError) as e:
        return _fail(e)
    log.info("user=%s tool=task_history count=%d", tenant.name, len(rows))
    return {"tasks": rows, "count": len(rows)}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def usage_summary(
    since: str = "7d",
    by: str = "model",
    headers: dict = _current_headers,
) -> dict:
    """Aggregate your task ledger: counts, failures, duration percentiles,
    tokens and cost.

    Group `by` model, role, day, worker, backend or kind. `cost_estimated` is
    true when any task ran on a subscription credential, where claude reports a
    notional cost rather than money billed.
    """
    try:
        tenant = tenant_from_headers(headers)
        rows = await asyncio.to_thread(
            ledger.records, tenant.ledger, ledger.MAX_RECORDS, since, None)
        summary = await asyncio.to_thread(ledger.summarize, rows, by)
    except (TenantError, ValueError) as e:
        return _fail(e)
    summary["since"] = since
    summary["truncated"] = len(rows) >= ledger.MAX_RECORDS
    log.info("user=%s tool=usage_summary by=%s tasks=%d",
             tenant.name, by, summary["totals"]["tasks"])
    return summary


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "openWorldHint": False})
async def register_credential(token: str, headers: dict = _current_headers) -> dict:
    """Register your own Anthropic credential, which your workers run under.

    Pass an API key (`sk-ant-api…`) or a Claude Code OAuth token
    (`sk-ant-oat…`); the kind is taken from the prefix. It is stored as a
    podman secret of the service user and replaces any previous one of that
    kind. It is never logged or echoed back.

    Note what this costs: the value passes through this conversation and
    Anthropic's connector relay on its way here. Prefer a dedicated,
    spend-capped key you can revoke on its own.
    """
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    try:
        ratelimit.check(tenant.name, "register_credential")
        result = await selfservice.register_credential(tenant, token)
    except (TenantError, ratelimit.RateLimited) as e:
        log.info("user=%s tool=register_credential outcome=refused", tenant.name)
        return _fail(e)
    log.info("user=%s tool=register_credential outcome=stored kind=%s",
             tenant.name, result["registered"])
    return result


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "openWorldHint": False})
async def revoke_credential(kind: str, headers: dict = _current_headers) -> dict:
    """Delete one of your stored credentials (`apikey` or `oauth`). Workers
    already running keep theirs until they retire."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    try:
        result = await selfservice.revoke_credential(tenant, kind)
    except TenantError as e:
        log.info("user=%s tool=revoke_credential outcome=refused", tenant.name)
        return _fail(e)
    log.info("user=%s tool=revoke_credential outcome=revoked kind=%s existed=%s",
             tenant.name, kind, result["existed"])
    return result


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "openWorldHint": False})
async def set_password(new_password: str, headers: dict = _current_headers) -> dict:
    """Change your own login password for this service's endpoint.

    At least 12 characters. It is stored only as a bcrypt hash, and takes
    effect immediately — so update the connector's saved credential right
    after, or your next call will fail to authenticate.
    """
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    try:
        ratelimit.check(tenant.name, "set_password")
        result = await selfservice.set_password(tenant, new_password)
    except (TenantError, ratelimit.RateLimited) as e:
        log.info("user=%s tool=set_password outcome=refused", tenant.name)
        return _fail(e)
    log.info("user=%s tool=set_password outcome=updated", tenant.name)
    return result


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": False})
async def set_git_identity(
    name: str, email: str, headers: dict = _current_headers
) -> dict:
    """Set the committer identity your implementer agents sign off commits
    with. Without one they cannot commit at all."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    try:
        result = await selfservice.set_git_identity(tenant, name, email)
    except TenantError as e:
        log.info("user=%s tool=set_git_identity outcome=refused", tenant.name)
        return _fail(e)
    log.info("user=%s tool=set_git_identity outcome=updated", tenant.name)
    return result


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def list_roles() -> dict:
    """List the worker roles you can spawn, with their workspace access."""
    return {
        "roles": {
            name: {"description": r.description, "workspace": r.workspace,
                   "image": r.image, "default_backend": r.default_backend}
            for name, r in ROLES.items()
        }
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def list_backends(headers: dict = _current_headers) -> dict:
    """Where workers can send their requests, and which models each accepts.

    Backends a backend is not available to you are omitted. `anthropic` is the
    hosted API under your own credential and accepts any current Claude model
    name; a backend with a `models` list accepts only those names, and
    `spawn_worker` refuses anything else rather than letting the worker fail
    its first task.
    """
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    out = {}
    for name, backend in sorted(backends.for_tenant(tenant.name).items()):
        out[name] = {
            "kind": backend.kind,
            # Empty means "whatever the provider accepts", which is only true
            # of the hosted API; it validates its own names.
            "models": list(backend.models),
            "default_model": backend.default_model,
            "task_timeout": backend.task_timeout,
            # A URL only, never a credential: `credential` names an operator
            # podman secret and its value never leaves the host.
            "endpoints": [e.url for e in backend.endpoints],
            # Honest about today: the registry can describe a local backend
            # before the spawn path can use one.
            "spawnable": backend.kind == "anthropic",
        }
    log.info("user=%s tool=list_backends count=%d", tenant.name, len(out))
    return {"backends": out}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def spawn_worker(
    role: str,
    count: int = 1,
    pool: bool = False,
    model: str | None = None,
    effort: str | None = None,
    backend: str | None = None,
    headers: dict = _current_headers,
) -> dict:
    """Start `count` workers of `role` (see list_roles). Each worker is a
    persistent pull-worker: it runs one task at a time, each in a fresh
    container under your own Anthropic credential.

    With pool=True the workers also consume the shared `<role>.work` queue, so
    tasks sent there are load-balanced across them. Each worker is always
    addressable directly as `<id>.inbox`.

    `model` and `effort` set the workers' defaults: model is an alias for the
    latest of a family (fable, opus, sonnet, haiku) or a full model id such as
    claude-opus-5-5; effort is low, medium, high, xhigh or max. Omitted, claude
    uses its own defaults. A single task can override both (see send_task).

    `backend` chooses where the workers send their requests (see
    list_backends); omitted, the role's default is used, which is the hosted
    Anthropic API.
    """
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    if role not in ROLES:
        return _fail(f"unknown role {role!r}; available: {', '.join(ROLES)}")

    try:
        chosen = backends.get(backend or ROLES[role].default_backend)
    except backends.BackendError as e:
        return _fail(e)
    if not chosen.allows(tenant.name):
        # Say it is not yours rather than that it does not exist: the operator
        # chose the restriction and a caller cannot act on a lie.
        return _fail(f"backend {chosen.name!r} is not available to "
                     f"{tenant.name!r}")
    if chosen.kind != "anthropic":
        # The registry can describe a local backend before the spawn path can
        # use one. Saying so is better than spawning a worker that fails every
        # task it is given.
        return _fail(
            f"backend {chosen.name!r} is kind {chosen.kind!r}, which this "
            f"service can describe but not yet spawn on — only 'anthropic' "
            f"works today (PLAN.md build step 12)")

    try:
        options = validate_task_options(
            model, effort, list(chosen.models) or None)
    except ValueError as e:
        return _fail(e)
    if count < 1:
        return _fail("count must be at least 1")
    credential = await _credential(tenant)
    if credential is None:
        return _fail(
            f"no Anthropic credential registered for {tenant.name!r}: the "
            f"operator must create podman secret {tenant.secret_name('apikey')!r} "
            f"or {tenant.secret_name('oauth')!r} for the service user"
        )
    if not config.CLAUDE_BIN or not Path(config.CLAUDE_BIN).is_file():
        raise RuntimeError(f"CLAUDE_BIN is not a file: {config.CLAUDE_BIN!r}")

    existing = await workers.list_units(tenant)
    active = [w for w in existing if w["active"] in ("active", "activating")]
    if len(active) + count > tenant.max_workers:
        return _fail(
            f"worker limit reached: {len(active)} running, limit "
            f"{tenant.max_workers}; retire some workers first"
        )

    # Declare the collector before any worker can produce output.
    await broker.ensure_collector(tenant)
    ids = [w["id"] for w in existing]
    started = []
    for _ in range(count):
        n = workers.next_index(ids, role)
        await workers.start(
            workers.build_command(tenant, role, n, credential, pool, options))
        agent_id = f"{role}-{n}"
        ids.append(agent_id)
        started.append({
            "id": agent_id,
            "inbox": f"{agent_id}.inbox",
            "work_queue": f"{role}.work" if pool else None,
            "model": options.get("model"),
            "effort": options.get("effort"),
        })
    log.info("user=%s tool=spawn_worker role=%s backend=%s workers=%s options=%s",
             tenant.name, role, chosen.name,
             ",".join(w["id"] for w in started), options or "default")
    return {"workers": started, "backend": chosen.name}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def list_workers(headers: dict = _current_headers) -> dict:
    """List your worker units and their systemd state.

    Running checks share the worker budget but are not listed here — see
    `fleet_status` for those."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    units = await workers.list_units(tenant)
    return {"workers": [u for u in units if u.get("kind") != "check"],
            "checks_running": sum(1 for u in units if u.get("kind") == "check")}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def send_task(
    to: str,
    body: str,
    model: str | None = None,
    effort: str | None = None,
    expect: list[str] | None = None,
    headers: dict = _current_headers,
) -> dict:
    """Send a task to one worker (`<id>.inbox`) or to a role pool
    (`<role>.work`). The agent is stateless — include every piece of context it
    needs in `body`. Returns a task_id; its output and a final `done` entry
    (with the exit code) appear in collect().

    `model`/`effort` override the worker's defaults for this task only (same
    values as spawn_worker) — e.g. haiku for quick triage, opus at high effort
    for a hard task.

    `expect` declares the files the task must leave in its own output
    directory (mounted at `/out` in the container, writable even for
    read-only roles), e.g. `expect=["review-result.md"]`. The host checks them
    after the agent exits: `done.ok` is true only if the exit code was 0 **and**
    every declared file exists, is non-empty, is a real file and was written by
    this task. Prefer this to trusting an agent's own account of what it
    produced; `collect` returns the files."""
    try:
        tenant = tenant_from_headers(headers)
        options = validate_task_options(model, effort)
        declared = expect_mod.validate(expect)
        # Checked before dispatch: the limit stops new work starting, it
        # cannot cap a task already running.
        await asyncio.to_thread(budget.check, tenant)
    except (TenantError, ValueError, expect_mod.ExpectError,
            budget.BudgetExceeded) as e:
        return _fail(e)
    if to.endswith(".inbox"):
        if to.removesuffix(".inbox") not in await _worker_ids(tenant):
            return _fail(f"no worker {to.removesuffix('.inbox')!r}; see list_workers")
    elif to.endswith(".work"):
        if to.removesuffix(".work") not in ROLES:
            return _fail(f"no role pool {to!r}; available roles: {', '.join(ROLES)}")
    else:
        return _fail("`to` must be `<worker id>.inbox` or `<role>.work`")
    task_id = broker.new_task_id()
    message = {"body": body, "task_id": task_id, **options}
    if declared:
        message["expect"] = declared
    await broker.publish(tenant, to, message)
    await broker.publish_dispatch(tenant, task_id, to)
    log.info("user=%s tool=send_task to=%s task=%s bytes=%d options=%s", tenant.name,
             to, task_id, len(body.encode()), options or "default")
    return {"sent": True, "routing_key": to, "task_id": task_id,
            **({"expect": declared} if declared else {})}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def broadcast(
    body: str,
    model: str | None = None,
    effort: str | None = None,
    expect: list[str] | None = None,
    headers: dict = _current_headers,
) -> dict:
    """Send `body` as a task to every one of your workers at once (optionally
    overriding model/effort, as in send_task).

    `expect` is **refused** here: every worker runs a broadcast under the same
    task_id, so they would share one `/out` directory and silently overwrite
    each other's file — and the host would report success because *a* valid
    file exists. To collect a file from each worker, send_task to each one
    with a distinct name.
    """
    try:
        tenant = tenant_from_headers(headers)
        options = validate_task_options(model, effort)
        await asyncio.to_thread(budget.check, tenant)
    except (TenantError, ValueError, budget.BudgetExceeded) as e:
        return _fail(e)
    if expect:
        return _fail(
            "broadcast does not accept expect: every worker would write to the "
            "same /out directory and overwrite the others, and the result would "
            "look like a success. Use send_task per worker with distinct names."
        )
    task_id = broker.new_task_id()
    message = {"body": body, "task_id": task_id, **options}
    await broker.publish(tenant, "orchestrator.broadcast", message)
    await broker.publish_dispatch(tenant, task_id, "orchestrator.broadcast")
    log.info("user=%s tool=broadcast task=%s bytes=%d", tenant.name, task_id,
             len(body.encode()))
    return {"sent": True, "task_id": task_id}


def _declared_files(tenant, entry: dict) -> dict:
    """Return each declared output that passed: inline when small, else a ref.

    A ref is a workspace-relative path, so `workspace_read` can fetch the rest
    without this result carrying a megabyte of it.
    """
    task_id = entry.get("task_id")
    files: dict[str, dict] = {}
    if not task_id:
        return files
    for name, status in (entry.get("expect") or {}).items():
        if status != expect_mod.OK:
            files[name] = {"status": status}
            continue
        rel = f"{workspace.OUTBOX}/{task_id}/{name}"
        try:
            path = workspace.resolve(tenant, rel)
            size = path.stat().st_size
        except (OSError, WorkspaceError) as exc:
            files[name] = {"status": "unreadable", "error": str(exc)}
            continue
        row: dict = {"status": expect_mod.OK, "bytes": size, "ref": rel}
        if size <= config.INLINE_MAX_BYTES:
            row["text"] = path.read_text(encoding="utf-8", errors="replace")
        files[name] = row
    return files


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def collect(
    idle: float = 30,
    done: int = 0,
    until: str | None = None,
    max_seconds: float = config.MAX_COLLECT_SECONDS,
    max_messages: int = 1000,
    headers: dict = _current_headers,
) -> dict:
    """Collect results from your workers. Blocks until no message has arrived
    for `idle` seconds, `done` tasks have finished, a line contains `until`, or
    `max_seconds` pass (capped by the server). Returns one entry per task —
    `{agent_id, task_id, done, rc, body}` — plus `waiting` entries for idle
    workers. A task with done=false is still running: call collect again.
    Bodies larger than a few KiB come back as `{ref, bytes}`; read them with
    workspace_read(ref)."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    max_seconds = max(1.0, min(max_seconds, config.MAX_COLLECT_SECONDS))
    idle = max(1.0, min(idle, max_seconds))
    raw, stopped_by = await broker.drain(
        tenant, idle=idle, max_seconds=max_seconds,
        max_messages=max(1, max_messages), until=until, done=max(0, done),
    )
    results = fold_messages(raw)
    token = None
    for i, entry in enumerate(results):
        body = entry.get("body")
        if body is not None and len(body.encode()) > config.INLINE_MAX_BYTES:
            token = token or workspace.new_outbox_token()
            entry["ref"] = workspace.outbox_put(tenant, token, i, body)
            entry["bytes"] = len(body.encode())
            del entry["body"]
        if entry.get("expect"):
            entry["files"] = await asyncio.to_thread(
                _declared_files, tenant, entry)
    log.info("user=%s tool=collect messages=%d results=%d stopped_by=%s",
             tenant.name, len(raw), len(results), stopped_by)
    return {"results": results, "stopped_by": stopped_by}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "openWorldHint": False})
async def retire_worker(
    worker_id: str, force: bool = False, headers: dict = _current_headers
) -> dict:
    """Retire one worker: it finishes any task in flight, then exits. With
    force=True its unit is stopped immediately, killing a running task."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    if worker_id not in await _worker_ids(tenant):
        return _fail(f"no worker {worker_id!r}; see list_workers")
    if force:
        await workers.stop(tenant, worker_id)
    else:
        await broker.publish(tenant, f"{worker_id}.inbox", {"type": "shutdown"})
    log.info("user=%s tool=retire_worker id=%s force=%s", tenant.name, worker_id, force)
    return {"retired": [worker_id], "forced": force}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "openWorldHint": False})
async def retire_all(force: bool = False, headers: dict = _current_headers) -> dict:
    """Retire all your workers (see retire_worker)."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    retired = []
    for w in await workers.list_units(tenant):
        if force:
            await workers.stop(tenant, w["id"])
        else:
            await broker.publish(tenant, f"{w['id']}.inbox", {"type": "shutdown"})
        retired.append(w["id"])
    log.info("user=%s tool=retire_all count=%d force=%s", tenant.name, len(retired), force)
    return {"retired": retired, "forced": force}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def workspace_ls(path: str = ".", headers: dict = _current_headers) -> dict:
    """List a directory in your workspace (mounted at /workspace in agents)."""
    try:
        tenant = tenant_from_headers(headers)
        return {"path": path, "entries": workspace.ls(tenant, path)}
    except (TenantError, WorkspaceError) as e:
        return _fail(e)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def workspace_read(
    path: str, max_bytes: int = 65536, headers: dict = _current_headers
) -> dict:
    """Read a text file from your workspace (e.g. a collect() `ref`)."""
    try:
        tenant = tenant_from_headers(headers)
        result = workspace.read(tenant, path, max_bytes)
    except (TenantError, WorkspaceError) as e:
        return _fail(e)
    log.info("user=%s tool=workspace_read bytes=%d", tenant.name, result["bytes"])
    return result


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "openWorldHint": False})
async def workspace_write(path: str, text: str, headers: dict = _current_headers) -> dict:
    """Write (create or overwrite) a text file in your workspace."""
    try:
        tenant = tenant_from_headers(headers)
        result = workspace.write(tenant, path, text)
    except (TenantError, WorkspaceError) as e:
        return _fail(e)
    log.info("user=%s tool=workspace_write bytes=%d", tenant.name, result["bytes"])
    return result


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def clone_repo(url: str, dest: str, headers: dict = _current_headers) -> dict:
    """Clone an https:// git repository into `dest` inside your workspace, so
    agents can work on it at /workspace/<dest>.

    A private repository works if a read credential is registered for it (see
    list_git_credentials); otherwise the clone is anonymous. The credential
    stays on the host: it is never written into the repository's config and
    never reaches a container.
    """
    try:
        tenant = tenant_from_headers(headers)
        host, repo = gitcred.parse_url(url)
        entry = gitcred.select(tenant, host, repo, scope="read")
        env = gitcred.git_env(tenant, entry)
        result = await workspace.clone(tenant, url, dest, env=env)
    except (TenantError, WorkspaceError, gitcred.GitCredError) as e:
        return _fail(e, gitcred.diagnose_git_http_error(str(e)))
    log.info("user=%s tool=clone_repo host=%s repo=%s %s", tenant.name, host,
             repo, gitcred.safe_log_fields(entry))
    if result.get("repaired"):
        log.warning("user=%s tool=clone_repo repaired a credential in "
                    "remote.origin.url", tenant.name)
    return {**result, "authenticated": entry is not None,
            **gitcred.safe_log_fields(entry)}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def list_git_credentials(headers: dict = _current_headers) -> dict:
    """The git credentials registered for you: host, which repositories they
    match, and whether they are read or write. Never a token value.

    Registering one is the operator's job, deliberately: a token passed as a
    tool argument would be recorded in this conversation. Ask them to run
    `agentbridge-admin git-token <you> --host github.com --match owner/name`
    as root; `docs/github-token.md` is the full instruction. Do not offer to
    accept the token yourself — in this conversation it would be disclosed.
    """
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    entries = await asyncio.to_thread(gitcred.list_credentials, tenant)
    log.info("user=%s tool=list_git_credentials count=%d", tenant.name,
             len(entries))
    return {"credentials": entries,
            "hosts_allowed": list(gitcred.ALLOWED_HOSTS)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def fetch_repo(dest: str, headers: dict = _current_headers) -> dict:
    """Refresh an existing clone in your workspace from its origin.

    Updates remote-tracking refs only (`origin/*` and tags) — your working
    tree, your branches and `HEAD` are untouched, so nothing an agent has
    committed is at risk. Merge or rebase is a task for an agent afterwards.

    Two steps, for one reason: the authenticated fetch happens on the host
    into a mirror the service owns, and a sandboxed container then fetches
    from that mirror into your workspace. The credential never touches a
    repository an agent can write, and that repository's own config is only
    ever interpreted inside the sandbox.
    """
    try:
        tenant = tenant_from_headers(headers)
        target = workspace.resolve(tenant, dest)
        if not target.is_dir():
            return _fail(f"{dest!r} is not a directory in your workspace")
        url = await asyncio.to_thread(gitcred.origin_url, target)
        host, repo = gitcred.parse_url(url)
        entry = gitcred.select(tenant, host, repo, scope="read")
    except (TenantError, WorkspaceError, gitcred.GitCredError) as e:
        return _fail(e)

    try:
        mirror = await gitcred.update_mirror(tenant, host, repo, url, entry)
        rel = str(target.relative_to(tenant.workspace.resolve()))
        role = checkpoint_mod.role_for_worker("implementer")
        argv = checkpoint_mod.fetch_command(tenant, role, rel, str(mirror))
        result = await checkpoint_mod.run_leaf(argv)
    except (gitcred.GitCredError, checkpoint_mod.CheckpointError) as e:
        # update_mirror is the authenticated step, so a forge rejection
        # surfaces here rather than from the sandboxed leaf.
        return _fail(e, gitcred.diagnose_git_http_error(str(e)))
    if result.get("error"):
        return _fail(f"the fetch leaf failed: {result['error']}")

    log.info("user=%s tool=fetch_repo host=%s repo=%s %s", tenant.name, host,
             repo, gitcred.safe_log_fields(entry))
    # The leaf's fields first, so nothing it reports can shadow the tool's
    # own answer: its "path" once collided with this "repo" and the result
    # named the workspace directory as though it were the repository.
    return {**result, "dest": dest, "repo": f"{host}/{repo}",
            "authenticated": entry is not None,
            "working_tree_unchanged": True}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def test_git_credential(
    repo_url: str, headers: dict = _current_headers
) -> dict:
    """Check that a repository can be reached with the credential that would
    be used for it — `git ls-remote`, which fetches no objects.

    Use this before `clone_repo` on a private repository, so a failure is a
    clear "the token cannot see it" rather than a confusing clone error.
    """
    try:
        tenant = tenant_from_headers(headers)
        host, repo = gitcred.parse_url(repo_url)
        entry = gitcred.select(tenant, host, repo, scope="read")
        env = gitcred.git_env(tenant, entry)
    except (TenantError, gitcred.GitCredError) as e:
        return _fail(e)
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
        "ls-remote", "--heads", "--", f"https://{host}/{repo}.git",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        return _fail("git ls-remote timed out after 30s")
    fields = gitcred.safe_log_fields(entry)
    log.info("user=%s tool=test_git_credential host=%s repo=%s rc=%s %s",
             tenant.name, host, repo, proc.returncode, fields)
    if proc.returncode != 0:
        # stderr from git can echo the URL but never the header we set.
        stderr = err.decode(errors="replace").strip()
        out = {"success": False, "repo": f"{host}/{repo}", **fields,
               "error": stderr[-400:]}
        diagnosis = gitcred.diagnose_git_http_error(stderr)
        if diagnosis:
            out["diagnosis"] = diagnosis
        return out
    return {"ok": True, "repo": f"{host}/{repo}", **fields,
            "branches": len([ln for ln in out.decode().splitlines() if ln.strip()])}


async def _serve_both(host: str, port: int, admin_port: int) -> None:
    """The user endpoint and, on its own port, the admin one (I-2).

    Two listeners in one process: they share this host's state, so a second
    unit would only add ways for the two to disagree. Both bind loopback;
    which of them is reachable from outside is nginx's decision.
    """
    from .admin_api import admin_mcp

    await asyncio.gather(
        mcp.run_async(transport="http", host=host, port=port, show_banner=False),
        admin_mcp.run_async(transport="http", host=host, port=admin_port,
                            show_banner=False),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-tenant MCP service running agentbridge agent fleets"
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8817")))
    parser.add_argument(
        "--admin-port", type=int,
        default=int(os.environ.get("ADMIN_PORT", "8818")),
        help="port for the admin endpoint (0 disables it entirely)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", "streamable-http"),
    )
    args = parser.parse_args()

    # A system unit with User= has no session environment; systemd-run --user,
    # systemctl --user and rootless podman need the user manager's runtime dir.
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus"
    )

    try:
        from systemd.journal import JournalHandler

        handler: logging.Handler = JournalHandler(SYSLOG_IDENTIFIER="mcp-agentbridge")
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)

    logging.basicConfig(handlers=[handler], level=logging.INFO)

    # Load the backend registry before serving. A bad backends.toml should stop
    # the service here, with the reason in the journal, rather than surface at
    # the first spawn_worker as a worker that cannot run anything.
    try:
        registry = backends.backends(reload=True)
    except backends.BackendError as exc:
        sys.exit(f"{config.BACKENDS_FILE}: {exc}")
    log.info("backends: %s", ", ".join(
        f"{name}({b.kind})" for name, b in sorted(registry.items())))

    if args.transport != "streamable-http":
        mcp.run(transport=args.transport)
    elif args.admin_port:
        log.info("serving user endpoint on %s:%d, admin on :%d",
                 args.host, args.port, args.admin_port)
        asyncio.run(_serve_both(args.host, args.port, args.admin_port))
    else:
        log.info("serving user endpoint on %s:%d, admin endpoint disabled",
                 args.host, args.port)
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
