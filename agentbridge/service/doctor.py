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

import ipaddress
import json
import os
import socket
import stat
import subprocess
import urllib.error
import urllib.parse
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
# A lister answers "what is in our nft table", as (rc, stdout, stderr). Reading
# the ruleset needs root, so a non-root caller gets a non-zero rc and a reason,
# which is a skip rather than a failure.
Lister = Callable[[], tuple[int, str, str]]

NFT_FAMILY = "inet"
NFT_TABLE = "agentbridge"
NFT_CHAIN = "leaf_egress"


def nft_lister(timeout: float = PROBE_TIMEOUT) -> tuple[int, str, str]:
    """Ask `nft` for our table as JSON.

    JSON rather than the text form on purpose: the text form is meant to be
    re-parsed by `nft`, not by us, and a check that scrapes it would start
    lying the first time nftables changed how it renders a rule.
    """
    try:
        proc = subprocess.run(
            ["nft", "-j", "list", "table", NFT_FAMILY, NFT_TABLE],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


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


def _elem_value(elem: object) -> str | None:
    """One set element's value. `nft -j` wraps it when it carries a comment."""
    if isinstance(elem, str):
        return elem
    if isinstance(elem, dict):
        inner = elem.get("elem", elem)
        if isinstance(inner, dict) and isinstance(inner.get("val"), str):
            return inner["val"]
    return None


def _nft_sets(parsed: dict) -> dict[str, set[str]]:
    """Every set in our table, as name -> element values."""
    out: dict[str, set[str]] = {}
    for item in parsed.get("nftables", []):
        if not isinstance(item, dict):
            continue
        spec = item.get("set")
        if not isinstance(spec, dict) or spec.get("table") != NFT_TABLE:
            continue
        values = {v for v in (_elem_value(e) for e in spec.get("elem") or []) if v}
        out[str(spec.get("name"))] = values
    return out


def _nft_chains(parsed: dict) -> set[str]:
    return {str(item["chain"].get("name"))
            for item in parsed.get("nftables", [])
            if isinstance(item, dict) and isinstance(item.get("chain"), dict)
            and item["chain"].get("table") == NFT_TABLE}


def _chain_has_drop(parsed: dict, chain: str) -> bool:
    """Does `chain` end a packet's journey with `drop` anywhere in it?

    This is how the two policy files are told apart without reading either of
    them: the enforcing one closes `leaf_egress` with `counter drop`, the audit
    one with a counter and a log and no verdict at all.
    """
    for item in parsed.get("nftables", []):
        if not isinstance(item, dict):
            continue
        rule = item.get("rule")
        if not isinstance(rule, dict) or rule.get("chain") != chain:
            continue
        for expr in rule.get("expr") or []:
            if isinstance(expr, dict) and "drop" in expr:
                return True
    return False


def _endpoint_addresses(url: str) -> tuple[list[str], str]:
    """The addresses an endpoint resolves to, and why not if it does not.

    A literal address is taken as it stands. A name is resolved, because what
    the policy has to allow is an address — but a resolution failure is DNS's
    problem, not the policy's, so it is reported and not blamed on the sets.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return [], f"no host in {url!r}"
    try:
        return [str(ipaddress.ip_address(host))], ""
    except ValueError:
        pass
    try:
        info = socket.getaddrinfo(host, None)
    except OSError as exc:
        return [], f"{host} does not resolve: {exc}"
    return sorted({str(entry[4][0]) for entry in info}), ""


def check_netpolicy(lister: Lister | None = None,
                    path: Path | None = None) -> list[Finding]:
    """Is the leaf egress policy loaded, and does its allowlist still match?

    Three separate questions, because they fail separately.

    **Is it there at all?** `agentbridge-nft.service` runs `nft -f` once and
    asserts nothing afterwards, so `active (exited)` means "that one load
    succeeded", not "the policy is in the kernel now". The unit would report
    success just as cheerfully with the ruleset flushed underneath it. On
    2026-11-13 the same shape cost this host six hours of LAN access, with
    systemd reporting success throughout.

    **Which file is loaded?** The audit policy logs and drops nothing; the
    enforcing one drops. Believing enforcement is on while the audit file is
    loaded is the quiet failure this check exists for, which is why the audit
    case is a `warn` and not an `ok` — `--quiet` hides `ok`, and this is
    precisely the line an operator must not stop seeing.

    **Does the allowlist still match `backends.toml`?** The sets are
    hand-written until E.4's `agentbridge-netpolicy` renders them, so they
    drift. A configured endpoint missing from the sets is a leaf that the
    enforcing policy will silently drop; an address left behind after a backend
    is removed is a permission nobody meant to keep.
    """
    rc, out, err = (lister or nft_lister)()
    if rc != 0:
        reason = (err or out or "nft failed").strip().splitlines()[-1]
        low = reason.lower()
        if "permitted" in low or "permission" in low or "denied" in low:
            return [Finding("leaf netpolicy", SKIP,
                            f"cannot read the ruleset as this user — run "
                            f"`agentbridge-admin doctor` as root ({reason})")]
        if "no such file" in low or "does not exist" in low:
            return [Finding("leaf netpolicy", FAIL,
                            f"table {NFT_FAMILY} {NFT_TABLE} is not loaded: "
                            f"the leaf egress policy is not in the kernel. "
                            f"`systemctl status agentbridge-nft.service` may "
                            f"still say active — it only ran `nft -f` once")]
        return [Finding("leaf netpolicy", FAIL, f"nft failed: {reason}")]

    try:
        parsed = json.loads(out or "{}")
    except ValueError:
        return [Finding("leaf netpolicy", FAIL,
                        "nft -j did not return JSON")]
    if not isinstance(parsed, dict):
        return [Finding("leaf netpolicy", FAIL,
                        "nft -j did not return JSON")]

    findings: list[Finding] = []
    chains = _nft_chains(parsed)
    if NFT_CHAIN not in chains:
        findings.append(Finding("leaf netpolicy", FAIL,
                                f"table {NFT_FAMILY} {NFT_TABLE} is loaded but "
                                f"has no {NFT_CHAIN} chain (has: "
                                f"{', '.join(sorted(chains)) or 'nothing'})"))
        return findings

    if _chain_has_drop(parsed, NFT_CHAIN):
        findings.append(Finding("leaf netpolicy", OK,
                                f"enforcing: {NFT_CHAIN} drops what it does not "
                                f"allow"))
    else:
        findings.append(Finding("leaf netpolicy", WARN,
                                f"audit only: {NFT_CHAIN} has no drop, so "
                                f"nothing is blocked — this is the dry-run "
                                f"file, not the enforcing one (step 0.1e)"))

    sets = _nft_sets(parsed)
    allowed = set().union(*sets.values()) if sets else set()
    try:
        loaded = backends_mod.load(path)
    except backends_mod.BackendError:
        findings.append(Finding("netpolicy allowlist", SKIP,
                                "not compared: backends.toml does not load"))
        return findings

    wanted: dict[str, str] = {}     # address -> the backend that wants it
    for name, backend in sorted(loaded.items()):
        if backend.kind == "anthropic":
            # Reached over the internet on 443, which the policy allows for
            # every leaf; it is not an allowlisted address.
            continue
        for endpoint in backend.endpoints:
            addresses, why = _endpoint_addresses(endpoint.url)
            if not addresses:
                findings.append(Finding(f"netpolicy {name}", SKIP,
                                        f"not compared: {why}"))
                continue
            if not any(a in allowed for a in addresses):
                findings.append(Finding(f"netpolicy {name}", FAIL,
                                        f"{endpoint.url} resolves to "
                                        f"{', '.join(addresses)}, none of which "
                                        f"is in the policy's allow sets — the "
                                        f"enforcing policy drops this backend"))
            else:
                findings.append(Finding(f"netpolicy {name}", OK,
                                        f"{endpoint.url} is allowed"))
            for address in addresses:
                wanted.setdefault(address, name)

    stale = sorted(a for a in allowed if a not in wanted)
    if stale:
        findings.append(Finding("netpolicy allowlist", WARN,
                                f"allowed but not in backends.toml: "
                                f"{', '.join(stale)} — a permission outliving "
                                f"the backend that needed it"))
    elif allowed:
        findings.append(Finding("netpolicy allowlist", OK,
                                f"{len(allowed)} allowed address(es), all "
                                f"declared in backends.toml"))
    else:
        findings.append(Finding("netpolicy allowlist", OK,
                                "empty, and no non-hosted backend is declared"))
    return findings


# ------------------------------------------------------------------ driving


def run_all(probe: Probe = http_probe, runner: Runner | None = None,
            path: Path | None = None,
            lister: Lister | None = None) -> list[Finding]:
    """Every check, in the order an operator would want to read them."""
    return [
        *check_backends(path),
        *check_paths(),
        *check_binaries(),
        *check_roles(path),
        *check_images(runner),
        # Before the reachability checks on purpose: if the policy blocks an
        # endpoint, "unreachable" below is the symptom and this is the cause.
        *check_netpolicy(lister, path),
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
