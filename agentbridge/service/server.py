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

from ..bridge import validate_task_options
from ..bus import fold_messages
from . import broker, config, gitconfig, ratelimit, selfservice, workers, workspace
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
keeping (workspace_write, your own notes) and let the rest go."""

mcp = FastMCP("agentbridge", instructions=INSTRUCTIONS)

# Dependency sentinel (B008: must not call it in default arg position)
_current_headers = CurrentHeaders()


def _fail(error: Exception | str) -> dict:
    return {"success": False, "error": str(error)}


async def _credential(tenant):
    return pick_credential(tenant, await podman_secret_names())


async def _worker_ids(tenant) -> set[str]:
    return {w["id"] for w in await workers.list_units(tenant)}


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
    }


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
    """List your worker units and their systemd state."""
    try:
        tenant = tenant_from_headers(headers)
    except TenantError as e:
        return _fail(e)
    return {"workers": await workers.list_units(tenant)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def send_task(
    to: str,
    body: str,
    model: str | None = None,
    effort: str | None = None,
    headers: dict = _current_headers,
) -> dict:
    """Send a task to one worker (`<id>.inbox`) or to a role pool
    (`<role>.work`). The agent is stateless — include every piece of context it
    needs in `body`. Returns a task_id; its output and a final `done` entry
    (with the exit code) appear in collect().

    `model`/`effort` override the worker's defaults for this task only (same
    values as spawn_worker) — e.g. haiku for quick triage, opus at high effort
    for a hard task."""
    try:
        tenant = tenant_from_headers(headers)
        options = validate_task_options(model, effort)
    except (TenantError, ValueError) as e:
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
    await broker.publish(tenant, to, {"body": body, "task_id": task_id, **options})
    log.info("user=%s tool=send_task to=%s task=%s bytes=%d options=%s", tenant.name,
             to, task_id, len(body.encode()), options or "default")
    return {"sent": True, "routing_key": to, "task_id": task_id}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "openWorldHint": True})
async def broadcast(
    body: str,
    model: str | None = None,
    effort: str | None = None,
    headers: dict = _current_headers,
) -> dict:
    """Send `body` as a task to every one of your workers at once (optionally
    overriding model/effort, as in send_task)."""
    try:
        tenant = tenant_from_headers(headers)
        options = validate_task_options(model, effort)
    except (TenantError, ValueError) as e:
        return _fail(e)
    task_id = broker.new_task_id()
    await broker.publish(tenant, "orchestrator.broadcast",
                         {"body": body, "task_id": task_id, **options})
    log.info("user=%s tool=broadcast task=%s bytes=%d", tenant.name, task_id,
             len(body.encode()))
    return {"sent": True, "task_id": task_id}


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
