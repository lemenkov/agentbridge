# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Deterministic checks: run a command, report its exit code.

The point of this is to stop asking an agent whether the tests passed. An
agent reporting on its own work is the least reliable witness available, and
"I ran the tests and they pass" is a sentence a language model will produce
whether or not it is true. A check is a process and an exit code.

Three properties make the answer trustworthy:

**No credential, no claude.** The container gets no podman secret, no claude
binary and no ``ANTHROPIC_*`` environment. Nothing in it can talk to a model,
so nothing in it can be talked into anything.

**No network.** ``--network=none``, so a check cannot reach the broker, the
LAN, or a package index. Dependencies come from the image, which makes a check
reproducible; the promotion rule in the README applies when a check needs a
tool twice.

**Writes are discarded.** The workspace is mounted as a podman overlay
(``:O``), so a build or a test can write caches and artefacts, and the
workspace is exactly as it was afterwards. Verified rootless on this host.

The log and the result live **outside the workspace**, beside the tenant
records. The plan put them in ``.outbox/checks/``, but an implementer agent has
write access to its workspace, and a check log an agent can rewrite is worth
nothing — the same reasoning that keeps the ledger out of the workspace.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sys
import time
from pathlib import Path

from .. import ledger
from . import config, workspace
from .roles import ROLES, Role
from .tenant import Tenant

# A check's unit is `agentbridge-<u>-check-<id>`, so it shares the tenant's
# unit prefix (and therefore its resource caps and worker budget) while
# staying distinguishable from a worker.
CHECK_PREFIX = "check-"

MAX_COMMAND_ARGS = 64
MAX_ARG_LENGTH = 4096
TAIL_BYTES = 8192
MIN_TIMEOUT, MAX_TIMEOUT = 1, 3600

_CHECK_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class CheckError(Exception):
    """A caller-fixable problem with a check (reported, not raised)."""


def new_check_id() -> str:
    return secrets.token_hex(6)


def validate_check_id(check_id: str) -> str:
    if not _CHECK_ID_RE.match(check_id or ""):
        raise CheckError(f"invalid check id {check_id!r}")
    return check_id


def is_check(agent_id: str) -> bool:
    return agent_id.startswith(CHECK_PREFIX)


def checks_dir(tenant: Tenant) -> Path:
    return tenant.checks_dir


def _paths(tenant: Tenant, check_id: str) -> tuple[Path, Path]:
    base = checks_dir(tenant)
    return base / f"{check_id}.log", base / f"{check_id}.json"


def validate_command(command: list[str]) -> list[str]:
    """An argv list, not a string: there is no shell to quote for.

    ``["sh", "-c", "…"]`` is allowed — that shell runs inside the sandbox,
    with no network and no credential, so it buys the caller convenience
    without buying them anything on the host.
    """
    if not isinstance(command, list) or not command:
        raise CheckError("command must be a non-empty list of arguments")
    if len(command) > MAX_COMMAND_ARGS:
        raise CheckError(f"command has more than {MAX_COMMAND_ARGS} arguments")
    for arg in command:
        if not isinstance(arg, str):
            raise CheckError("every command argument must be a string")
        if len(arg) > MAX_ARG_LENGTH:
            raise CheckError("a command argument is too long")
        if "\x00" in arg:
            raise CheckError("command arguments must not contain NUL")
    return command


def validate_timeout(timeout: int) -> int:
    if not MIN_TIMEOUT <= int(timeout) <= MAX_TIMEOUT:
        raise CheckError(f"timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds")
    return int(timeout)


def unit_name(tenant: Tenant, check_id: str) -> str:
    return f"{tenant.unit_prefix}{CHECK_PREFIX}{check_id}.service"


def build_command(
    tenant: Tenant, role_name: str, check_id: str, command: list[str],
    workdir: str, timeout: int,
) -> list[str]:
    """The full ``systemd-run`` argv for one check.

    Deliberately absent, by comparison with a worker: ``--secret``, the claude
    mount, any ``ANTHROPIC_*`` variable, and the default network.
    """
    role: Role = ROLES[role_name]
    log, result = _paths(tenant, check_id)
    host_workdir = workspace.resolve(tenant, workdir)
    if not host_workdir.is_dir():
        raise CheckError(f"workdir {workdir!r} is not a directory in your workspace")
    rel = host_workdir.relative_to(tenant.workspace.resolve())

    return [
        "systemd-run", "--user", "--quiet", "--collect",
        f"--unit={unit_name(tenant, check_id)}",
        f"--description=agentbridge check {check_id} for {tenant.name} ({role_name})",
        f"--property=MemoryMax={config.WORKER_MEMORY_MAX}",
        f"--property=CPUQuota={config.WORKER_CPU_QUOTA}",
        f"--property=TasksMax={config.WORKER_TASKS_MAX}",
        # systemd kills the unit at the deadline; the result file keeps its
        # last known state, which is how a timeout is reported.
        f"--property=RuntimeMaxSec={timeout}",
        "--property=Delegate=yes",
        sys.executable, "-m", "agentbridge.service.checkrunner",
        "--result", str(result), "--log", str(log), "--check-id", check_id,
        "--ledger", str(tenant.ledger), "--role", role_name,
        "--",
        "podman", "run", "--rm",
        "--cgroups=split",
        "--security-opt", "label=disable",
        "--security-opt", "no-new-privileges",
        # Offline and reproducible: no broker, no LAN, no package index.
        "--network=none",
        "--tmpfs", "/tmp:rw,size=512m",
        "--log-driver=none",
        # Caches and build output are allowed, and discarded with the overlay.
        "-e", "HOME=/tmp",
        "-e", "TMPDIR=/tmp",
        "-e", "XDG_CACHE_HOME=/tmp/.cache",
        "-v", f"{tenant.workspace}:/workspace:O",
        "-w", f"/workspace/{rel}" if str(rel) != "." else "/workspace",
        role.image,
        *command,
    ]


def read_result(tenant: Tenant, check_id: str) -> dict | None:
    """The unit's own record of how the check ended, or None if not yet written."""
    _, result = _paths(tenant, check_id)
    try:
        payload = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_tail(tenant: Tenant, check_id: str, max_bytes: int = TAIL_BYTES) -> dict:
    """The last bytes of the check's output, with the total size."""
    log, _ = _paths(tenant, check_id)
    try:
        size = log.stat().st_size
    except OSError:
        return {"tail": "", "log_bytes": 0, "truncated": False}
    want = max(1, min(int(max_bytes), config.WORKSPACE_READ_MAX))
    with open(log, "rb") as fh:
        if size > want:
            fh.seek(size - want)
        data = fh.read(want)
    return {
        "tail": data.decode("utf-8", errors="replace"),
        "log_bytes": size,
        "truncated": size > len(data),
    }


# `timeout` from coreutils: a check that ran out of time failed, and should
# count as a failure in usage_summary rather than as a blank.
TIMEOUT_RC = 124

TERMINAL_STATES = ("done", "timeout")


def _finalize_timeout(tenant: Tenant, check_id: str, result: dict) -> dict:
    """Record a check whose unit died without writing a verdict.

    The unit cannot do this itself — it was killed — so the first reader to
    notice writes the verdict and the ledger line. Rewriting the result file
    is what makes it happen once: the next reader sees a finished check.
    """
    from datetime import datetime, timezone

    ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
    duration = None
    started = result.get("started")
    if started:
        try:
            began = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            duration = round(
                (datetime.now(timezone.utc) - began).total_seconds(), 3)
        except ValueError:
            duration = None

    payload = {
        **result,
        "state": "timeout",
        "rc": TIMEOUT_RC,
        "ended": ended,
        "duration_s": duration,
        "note": "the check's unit is gone without a result — it hit its "
                "timeout or was killed",
    }
    _, path = _paths(tenant, check_id)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temp.replace(path)
    except OSError:
        return payload

    try:
        ledger.append(tenant.ledger, {
            "kind": "check",
            "ts_start": started,
            "ts_end": ended,
            "task_id": check_id,
            "role": result.get("role"),
            "rc": TIMEOUT_RC,
            "duration_ms": int(duration * 1000) if duration else None,
            "timed_out": True,
        })
    except OSError:
        pass
    return payload


def _state_from(tenant: Tenant, check_id: str, unit_present: bool) -> dict:
    """Merge the unit's result file with whether its unit still exists.

    The same question as the fleet view's: a result file saying ``running``
    with no unit behind it means the unit died — timed out, was killed, or the
    host rebooted — and saying ``running`` forever would be a lie.
    """
    result = read_result(tenant, check_id) or {}
    state = result.get("state")
    # Any terminal state is final. "done" alone is not enough: a finalized
    # timeout is also terminal, and treating it as unfinished would re-run
    # the finalisation and append a second ledger row on every read.
    if state in TERMINAL_STATES:
        return result
    if unit_present:
        return {**result, "state": "running"}
    if not result:
        raise CheckError(
            f"no such check {check_id!r} (or its result has been cleaned up)"
        )
    return _finalize_timeout(tenant, check_id, result)


async def status(
    tenant: Tenant, check_id: str, unit_present: bool, tail_bytes: int = TAIL_BYTES
) -> dict:
    """The caller-facing shape, for both ``run_check`` and ``check_result``."""
    validate_check_id(check_id)
    state = await asyncio.to_thread(_state_from, tenant, check_id, unit_present)
    tail = await asyncio.to_thread(read_tail, tenant, check_id, tail_bytes)
    return {
        "check_id": check_id,
        "state": state.get("state", "running"),
        "rc": state.get("rc"),
        "duration_s": state.get("duration_s"),
        "started": state.get("started"),
        **({"error": state["error"]} if state.get("error") else {}),
        **({"note": state["note"]} if state.get("note") else {}),
        **tail,
    }


async def wait_for(
    tenant: Tenant, check_id: str, seconds: float, unit_present,
    poll: float = 0.5,
) -> dict:
    """Poll until the check is finished or ``seconds`` elapse.

    ``unit_present`` is an async callable, so the caller decides how to ask
    systemd (and the tests can answer without it).
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        present = await unit_present()
        result = await status(tenant, check_id, present)
        if result["state"] != "running":
            return result
        if time.monotonic() >= deadline:
            return result
        await asyncio.sleep(poll)
