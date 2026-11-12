# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Check a deploy without spawning anything.

Every failure this module looks for used to be discovered the same way: a
worker spawns, takes a task, and fails it. That is the most expensive possible
diagnostic — it costs a container start, a model call and a confused
orchestrator, and the error that comes back names the symptom rather than the
missing file. A `backends.toml` typo, an unpulled model, a `claude` binary that
moved in an update: all of them are visible from the host, for free, before
anybody spawns a worker.

So `doctor` reads configuration and the filesystem, probes endpoints that are
reachable without a credential, and asks podman whether an image exists. It
**never starts a container and never sends a task** — a diagnostic that can
spend money or change state is one an operator will hesitate to run, and a
diagnostic nobody runs is worth nothing.

Three deliberate choices:

* **Findings, not exceptions.** One broken check must not hide the other nine;
  an operator wants the whole picture in one pass.
* **`skip` is a first-class result.** Run as a user who cannot reach the
  service's podman store, the image check cannot be performed — saying so, with
  the command that would work, is honest. Reporting `ok` for a check that did
  not run is the failure mode of every "health check" that has ever lied.
* **Only `fail` sets the exit code.** `warn` is for things an operator may have
  chosen (no password file yet on a fresh host); `fail` is for a deploy that
  cannot work.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import backends as backends_mod
from . import config
from .roles import ROLES

# Long enough for a loopback or LAN model server that is busy, short enough
# that an unreachable endpoint does not make `doctor` look hung. A backend that
# cannot answer in this time is a finding either way.
PROBE_TIMEOUT = 5.0

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass(frozen=True)
class Finding:
    check: str
    level: str
    detail: str


# A probe returns (http_status, body, error). `status is None` means the
# request never got an answer, and `error` says why. An HTTP error status is
# still an answer: a 401 from a model server proves it is listening, which is
# all reachability means here.
Probe = Callable[[str], tuple[int | None, bytes, str]]
# A runner is `admin._as_service_user`-shaped: argv in, completed process out.
Runner = Callable[..., object]


def http_probe(url: str, timeout: float = PROBE_TIMEOUT
               ) -> tuple[int | None, bytes, str]:
    """GET `url`, treating any HTTP status as an answer."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536), ""
    except urllib.error.HTTPError as exc:
        return exc.code, b"", ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, b"", str(getattr(exc, "reason", exc) or exc)


# ------------------------------------------------------------------- checks


def check_backends(path: Path | None = None) -> list[Finding]:
    """Does `backends.toml` parse, validate, and look like the file it claims?"""
    path = Path(path) if path is not None else config.BACKENDS_FILE
    out: list[Finding] = []
    try:
        loaded = backends_mod.load(path)
    except backends_mod.BackendError as exc:
        # The service refuses to start on this, so it is a fail, and the
        # message from `load` already names the backend and the key.
        return [Finding("backends.toml", FAIL, str(exc))]

    if not path.exists():
        out.append(Finding("backends.toml", OK,
                           f"{path} absent — hosted Anthropic API only, which "
                           f"is the documented default"))
    else:
        out.append(Finding("backends.toml", OK,
                           f"{path} parses: {', '.join(sorted(loaded))}"))
        st = path.stat()
        # Documented as root-owned 0644 and read by two services. Writable by
        # anyone else means a non-root user chooses where leaves send requests.
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            out.append(Finding("backends.toml mode", FAIL,
                               f"{path} is mode {stat.S_IMODE(st.st_mode):04o} — "
                               f"group- or world-writable; it decides where "
                               f"leaves send their requests"))
        elif st.st_uid != 0:
            out.append(Finding("backends.toml mode", WARN,
                               f"{path} is owned by uid {st.st_uid}, not root"))
        else:
            out.append(Finding("backends.toml mode", OK,
                               f"root-owned, mode {stat.S_IMODE(st.st_mode):04o}"))
    return out


def check_roles(path: Path | None = None) -> list[Finding]:
    """Every role's preamble must exist: the bridge reads these host-side and
    prepends them to each task, so a missing one is a worker that runs with no
    instructions at all rather than a worker that fails to start."""
    out: list[Finding] = []
    # A registry that does not load is already reported by check_backends, and
    # re-raising it here would hide every role finding behind it.
    try:
        known = set(backends_mod.load(path))
    except backends_mod.BackendError:
        known = None
    for name, role in sorted(ROLES.items()):
        missing, empty = [], []
        for preamble in role.preamble_files:
            if not preamble.is_file():
                missing.append(str(preamble))
            elif preamble.stat().st_size == 0:
                empty.append(str(preamble))
        if missing:
            out.append(Finding(f"role {name}", FAIL,
                               f"preamble missing: {', '.join(missing)}"))
        elif empty:
            out.append(Finding(f"role {name}", FAIL,
                               f"preamble is empty: {', '.join(empty)}"))
        else:
            files = ", ".join(p.name for p in role.preamble_files)
            out.append(Finding(f"role {name}", OK,
                               f"{files}; image {role.image}; "
                               f"workspace {role.workspace}; "
                               f"backend {role.default_backend}"))
        if known is not None and role.default_backend not in known:
            out.append(Finding(f"role {name}", FAIL,
                               f"default_backend {role.default_backend!r} is "
                               f"not a backend this host has"))
    return out


def _check_binary(label: str, value: str, hint: str) -> Finding:
    if not value:
        return Finding(label, FAIL, f"not set — {hint}")
    path = Path(value)
    if not path.exists():
        return Finding(label, FAIL, f"{path} does not exist")
    if not path.is_file():
        return Finding(label, FAIL, f"{path} is not a file")
    if not os.access(path, os.X_OK):
        return Finding(label, FAIL, f"{path} is not executable")
    return Finding(label, OK, str(path))


def check_binaries() -> list[Finding]:
    """The two executables a worker cannot start without."""
    return [
        _check_binary("claude binary", config.CLAUDE_BIN,
                      "set CLAUDE_BIN in the sysconfig file; it is bind-mounted "
                      "read-only into every leaf"),
        _check_binary("bridge binary", config.BRIDGE_BIN,
                      "the worker process itself, normally in the service venv"),
    ]


def check_paths() -> list[Finding]:
    """Directories and files the service needs to have been laid down for it."""
    out: list[Finding] = []
    workspaces = config.WORKSPACES_DIR
    if not workspaces.is_dir():
        out.append(Finding("workspaces dir", FAIL,
                           f"{workspaces} is not a directory"))
    else:
        out.append(Finding("workspaces dir", OK, str(workspaces)))

    sysconfig = Path(config.SYSCONFIG_PATH)
    out.append(Finding("sysconfig", OK if sysconfig.is_file() else WARN,
                       str(sysconfig) if sysconfig.is_file()
                       else f"{sysconfig} absent — the service is running on "
                            f"defaults for every path"))

    htpasswd = config.HTPASSWD_FILE
    out.append(Finding("htpasswd", OK if htpasswd.is_file() else WARN,
                       str(htpasswd) if htpasswd.is_file()
                       else f"{htpasswd} absent — no tenant has a login yet"))

    # Named, never printed: this file is how the service talks to the broker.
    if config.RABBITMQ_ADMIN_USER and config.RABBITMQ_ADMIN_PASSWORD:
        out.append(Finding("broker admin credentials", OK,
                           f"set for {config.RABBITMQ_ADMIN_USER!r}"))
    else:
        out.append(Finding("broker admin credentials", FAIL,
                           f"RABBITMQ_ADMIN_USER/PASSWORD not set in "
                           f"{config.SYSCONFIG_PATH} — provisioning a tenant "
                           f"will fail"))
    return out


def _tags_url(endpoint_url: str, kind: str) -> str:
    # Each kind's cheapest unauthenticated "are you there" endpoint.
    return (f"{endpoint_url}/api/tags" if kind == "ollama"
            else f"{endpoint_url}/v1/models")


def check_endpoints(probe: Probe = http_probe,
                    path: Path | None = None) -> list[Finding]:
    """Can each backend's endpoints be reached at all?

    The hosted Anthropic backend is skipped on purpose: reaching it needs a
    tenant's credential, and `doctor` is not the place to spend one.
    """
    out: list[Finding] = []
    try:
        loaded = backends_mod.load(path)
    except backends_mod.BackendError:
        return [Finding("endpoints", SKIP,
                        "not checked: backends.toml does not load")]
    for name, backend in sorted(loaded.items()):
        if backend.kind == "anthropic":
            out.append(Finding(f"backend {name}", SKIP,
                               "hosted API: reaching it needs a tenant "
                               "credential, which doctor does not use"))
            continue
        for endpoint in backend.endpoints:
            url = _tags_url(endpoint.url, backend.kind)
            status, _body, error = probe(url)
            if status is None:
                out.append(Finding(f"backend {name}", FAIL,
                                   f"{endpoint.url} unreachable: {error}"))
            else:
                out.append(Finding(f"backend {name}", OK,
                                   f"{endpoint.url} answered HTTP {status}"))
    return out


def check_models(probe: Probe = http_probe,
                 path: Path | None = None) -> list[Finding]:
    """Is every model a backend allows actually present on its endpoint?

    An allowed-but-unpulled model is the failure this check exists for: the
    model name passes the bridge's allowlist, the task starts, and the pull
    happens inside the task — on a first call that then runs for minutes, or
    fails outright on a host with no route to a registry.
    """
    out: list[Finding] = []
    try:
        loaded = backends_mod.load(path)
    except backends_mod.BackendError:
        return [Finding("models", SKIP,
                        "not checked: backends.toml does not load")]
    for name, backend in sorted(loaded.items()):
        if backend.kind != "ollama":
            # Only ollama publishes an inventory without a credential.
            continue
        for endpoint in backend.endpoints:
            status, body, error = probe(f"{endpoint.url}/api/tags")
            if status is None:
                out.append(Finding(f"models {name}", SKIP,
                                   f"{endpoint.url} unreachable: {error}"))
                continue
            try:
                have = {m.get("name", "")
                        for m in json.loads(body or b"{}").get("models", [])}
            except (ValueError, AttributeError, TypeError):
                out.append(Finding(f"models {name}", FAIL,
                                   f"{endpoint.url}/api/tags did not return the "
                                   f"expected JSON"))
                continue
            # Ollama answers with a fully qualified tag; a bare name in the
            # config means `:latest`, which is the same model.
            have |= {n.split(":", 1)[0] for n in have if n.endswith(":latest")}
            absent = [m for m in backend.models
                      if m not in have and f"{m}:latest" not in have]
            if absent:
                out.append(Finding(f"models {name}", FAIL,
                                   f"{endpoint.url} does not have: "
                                   f"{', '.join(absent)} (it has "
                                   f"{', '.join(sorted(have)) or 'nothing'})"))
            else:
                out.append(Finding(f"models {name}", OK,
                                   f"{endpoint.url} has all "
                                   f"{len(backend.models)} allowed model(s)"))
    return out


def check_images(runner: Runner | None = None) -> list[Finding]:
    """Does each role's image exist in the **service user's** podman store?

    `podman image exists` inspects local storage and starts nothing. Run by a
    user who is not the service user, this would inspect the wrong store
    entirely, so without a runner that reaches it the check is skipped rather
    than answered from the wrong place.
    """
    images = sorted({role.image for role in ROLES.values()})
    if runner is None:
        return [Finding("role images", SKIP,
                        f"not checked (needs the service user's podman store): "
                        f"{', '.join(images)} — run `agentbridge-admin doctor` "
                        f"as root")]
    out: list[Finding] = []
    for image in images:
        result = runner("podman", "image", "exists", image)
        rc = getattr(result, "returncode", 1)
        if rc == 0:
            out.append(Finding(f"image {image}", OK, "present"))
        else:
            out.append(Finding(f"image {image}", FAIL,
                               "absent from the service user's podman store — "
                               "build it before spawning this role"))
    return out


# ------------------------------------------------------------------ driving


def run_all(probe: Probe = http_probe, runner: Runner | None = None,
            path: Path | None = None) -> list[Finding]:
    """Every check, in the order an operator would want to read them."""
    return [
        *check_backends(path),
        *check_paths(),
        *check_binaries(),
        *check_roles(path),
        *check_images(runner),
        *check_endpoints(probe, path),
        *check_models(probe, path),
    ]


def worst(findings: Sequence[Finding]) -> str:
    for level in (FAIL, WARN, SKIP):
        if any(f.level == level for f in findings):
            return level
    return OK


def exit_code(findings: Sequence[Finding]) -> int:
    """1 if anything failed. A warning or a skip is not a broken deploy."""
    return 1 if any(f.level == FAIL for f in findings) else 0
