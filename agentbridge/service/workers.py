# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Worker lifecycle: each worker is a transient systemd user unit of the
service user, running one agentbridge pull-worker whose per-task containers
live inside the unit's cgroup (``podman --cgroups=split``), so the unit's
MemoryMax/CPUQuota/TasksMax cap the whole worker."""

import asyncio
import json
import re

from ..bridge import option_args
from . import checks, config, workspace
from .roles import ROLES, Role
from .tenant import Tenant


async def _run(*argv: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def unit_name(tenant: Tenant, agent_id: str) -> str:
    return f"{tenant.unit_prefix}{agent_id}.service"


def build_command(
    tenant: Tenant,
    role_name: str,
    n: int,
    credential: tuple[str, str],
    pool: bool,
    options: dict[str, str] | None = None,
) -> list[str]:
    """The full ``systemd-run`` argv for one worker. No secret appears in it:
    the bus URL is read from a 0600 file and the Anthropic credential is
    injected by podman from its secret store."""
    role: Role = ROLES[role_name]
    agent_id = f"{role_name}-{n}"
    unit = unit_name(tenant, agent_id)
    secret, env_var = credential
    options = options or {}
    shown = " ".join(f"{k}={v}" for k, v in options.items()) or "default model"

    cmd = [
        "systemd-run", "--user", "--quiet", "--collect",
        f"--unit={unit}",
        f"--description=agentbridge worker {agent_id} for {tenant.name} ({shown})",
        f"--property=MemoryMax={config.WORKER_MEMORY_MAX}",
        f"--property=CPUQuota={config.WORKER_CPU_QUOTA}",
        f"--property=TasksMax={config.WORKER_TASKS_MAX}",
        "--property=Delegate=yes",
        config.BRIDGE_BIN,
        "--agent", agent_id,
        "--inbox", f"{agent_id}.inbox",
        "--exchange", config.EXCHANGE,
        "--amqp-url-file", str(tenant.amqp_url_file),
        "--idle-timeout", str(config.IDLE_TIMEOUT),
        "--driver", config.AGENT_DRIVER,
        # Metadata only, and outside the workspace the agent can write.
        "--ledger-file", str(tenant.ledger),
        "--role", role_name,
        "--status-file", str(tenant.status_dir / f"{agent_id}.json"),
        # Per-task output directory. The bridge creates it and substitutes
        # {task_id} here and in the podman argv below.
        "--out-template", str(tenant.workspace / workspace.OUTBOX / "{task_id}"),
        # Defaults for this worker; tasks may override them (allowlisted).
        *option_args(options),
        "--allow-task-overrides",
    ]
    if pool:
        cmd += ["--work-queue", f"{role_name}.work"]
    for path in role.preamble_files:
        cmd += ["--preamble-file", str(path)]

    # The wrapped command follows directly — no `--` separator.
    cmd += [
        "podman", "run", "--rm", "--interactive",
        "--cgroups=split",
        "--security-opt", "label=disable",
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,size=512m",
        # The bridge reads the leaf's stdout over the attach stream; a log
        # driver would also copy task output into the journal.
        "--log-driver=none",
        "-e", "IS_SANDBOX=1",
        "-e", "DISABLE_AUTOUPDATER=1",
        "--secret", f"{secret},type=env,target={env_var}",
        "-v", f"{config.CLAUDE_BIN}:/usr/local/bin/claude:ro",
        "-v", f"{tenant.workspace}:/workspace:{role.workspace}",
        # Writable even for a read-only role: that is the point — a reviewer
        # delivers a verdict without write access to what it reviewed.
        "-v", f"{tenant.workspace / workspace.OUTBOX}/{{task_id}}:/out:rw",
        "-w", "/workspace",
    ]
    if role.workspace == "rw" and tenant.gitconfig.exists():
        cmd += ["-v", f"{tenant.gitconfig}:/root/.gitconfig:ro"]
    cmd += [role.image, "claude", "--print", "--dangerously-skip-permissions"]
    return cmd


_DESC_OPTIONS = re.compile(r"\(([^()]*)\)\s*$")


def _options_from_description(description: str) -> str | None:
    """The "(model=opus effort=high)" suffix build_command puts in the unit
    description, without the parentheses."""
    m = _DESC_OPTIONS.search(description)
    return m.group(1) if m else None


async def list_units(tenant: Tenant) -> list[dict]:
    """The tenant's worker units, as ``{id, unit, active, sub}``."""
    rc, out, err = await _run(
        "systemctl", "--user", "list-units", f"{tenant.unit_prefix}*",
        "--all", "--output=json", "--no-pager",
    )
    if rc != 0:
        raise RuntimeError(f"systemctl list-units failed: {err.strip()}")
    workers = []
    for unit in json.loads(out or "[]"):
        name = unit.get("unit", "")
        # Only units with exactly our prefix — never another tenant's.
        if not name.startswith(tenant.unit_prefix) or not name.endswith(".service"):
            continue
        agent_id = name.removeprefix(tenant.unit_prefix).removesuffix(".service")
        workers.append({
            "id": agent_id,
            "unit": name,
            "active": unit.get("active"),
            "sub": unit.get("sub"),
            # A check shares the tenant's unit prefix — and so its resource
            # caps and worker budget — but is not a worker you can send tasks
            # to. Callers filter on this.
            "kind": "check" if checks.is_check(agent_id) else "worker",
            "options": _options_from_description(unit.get("description", "")),
        })
    return workers


async def list_all_units() -> list[dict]:
    """Every tenant's worker units — for the admin endpoint's `list_users`.

    One `systemctl` call covers the whole service, because all workers are
    user units of the one service user.
    """
    rc, out, err = await _run(
        "systemctl", "--user", "list-units", "agentbridge-*",
        "--all", "--output=json", "--no-pager",
    )
    if rc != 0:
        raise RuntimeError(f"systemctl list-units failed: {err.strip()}")
    return [
        {"unit": name, "active": unit.get("active"), "sub": unit.get("sub")}
        for unit in json.loads(out or "[]")
        if (name := unit.get("unit", "")).endswith(".service")
    ]


def next_index(existing_ids: list[str], role_name: str) -> int:
    pattern = re.compile(rf"^{re.escape(role_name)}-(\d+)$")
    used = [int(m.group(1)) for i in existing_ids if (m := pattern.match(i))]
    return max(used, default=0) + 1


async def start(cmd: list[str]) -> None:
    rc, _, err = await _run(*cmd)
    if rc != 0:
        raise RuntimeError(f"systemd-run failed (rc={rc}): {err.strip()}")


async def stop(tenant: Tenant, agent_id: str) -> None:
    rc, _, err = await _run("systemctl", "--user", "stop", unit_name(tenant, agent_id))
    if rc != 0:
        raise RuntimeError(f"systemctl stop failed (rc={rc}): {err.strip()}")
