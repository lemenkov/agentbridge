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
    broker,
    budget,
    checks,
    config,
    fleet,
    gitconfig,
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


def _fail(error: Exception | str) -> dict:
    return {"success": False, "error": str(error)}


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
            f"no checkpoint found for task {task_id!r} in your workspace — "
            f"only tasks on read-write roles are checkpointed, and an agent "
            f"can delete its own refs"
        )
    log.info("user=%s tool=rollback_workspace task=%s repos=%d force=%s",
             tenant.name, task_id, len(repos), force)
    return {
        "task_id": task_id,
        "repos": repos,
        "rolled_back": [r["repo"] for r in repos
                        if str(r.get("status", "")).startswith(("reset",
                                                                "branch_reset"))],
        "refused": [r["repo"] for r in repos
                    if r.get("status") == "refused_dirty"],
        "untracked_files_kept": True,
    }


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
                   "image": r.image}
            for name, r in ROLES.items()
        }
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def spawn_worker(
    role: str,
    count: int = 1,
    pool: bool = False,
    model: str | None = None,
    effort: str | None = None,
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
    """
    try:
        tenant = tenant_from_headers(headers)
        options = validate_task_options(model, effort)
    except (TenantError, ValueError) as e:
        return _fail(e)
    if role not in ROLES:
        return _fail(f"unknown role {role!r}; available: {', '.join(ROLES)}")
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
    log.info("user=%s tool=spawn_worker role=%s workers=%s options=%s", tenant.name,
             role, ",".join(w["id"] for w in started), options or "default")
    return {"workers": started}


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
    agents can work on it at /workspace/<dest>."""
    try:
        tenant = tenant_from_headers(headers)
        result = await workspace.clone(tenant, url, dest)
    except (TenantError, WorkspaceError) as e:
        return _fail(e)
    log.info("user=%s tool=clone_repo", tenant.name)
    return result


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
