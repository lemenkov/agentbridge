# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Git credentials for private repositories, kept on the host (I-4).

A credential here is a fine-grained forge token, scoped to chosen repositories
and — for anything an agent can trigger — read-only. Four rules shape the
module, and each exists because of a way this could go wrong:

**The secret never reaches a leaf.** Not as an environment variable, a mount
or a podman secret. An unattended agent running with
``--dangerously-skip-permissions`` and holding a token is one prompt injection
away from exfiltrating it.

**The secret never reaches a URL.** A token in a clone URL persists in
``.git/config``, which every agent in that workspace can read. It is passed
per invocation through ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n`` instead, so
it is absent from argv (visible in ``ps``), from the URL and from any file.

**The secret never reaches a conversation.** Registration is a root CLI
command reading the value from stdin. There is deliberately no MCP tool that
takes a token as an argument: a tool argument is a transcript entry, which is
how this host's Basic auth password leaked once already.

**Read and write are different credentials.** ``clone_repo`` and
``fetch_repo`` may use only a ``read`` credential. A ``write`` one is
reachable solely from a tool that asks the human first. One credential that
can do both turns an agent's mistake into a force-push.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .tenant import Tenant

# Forges the service will talk to at all. A host outside this list is refused
# before any credential is considered.
ALLOWED_HOSTS = tuple(
    h for h in os.environ.get(
        "GIT_HOSTS", "github.com gitlab.com codeberg.org").split() if h
)

# The username half of HTTP basic auth, per forge. The token is the password.
TOKEN_USERS = {
    "github.com": "x-access-token",
    "gitlab.com": "oauth2",
    "codeberg.org": "oauth2",
}

# Accepted token shapes, so a typo or a pasted password fails at registration
# rather than as a confusing 401 later. The prefix decides nothing about
# permissions — only the forge knows those — it only catches obvious mistakes.
TOKEN_PREFIXES = (
    ("github_pat_", "github fine-grained"),
    ("ghp_", "github classic (discouraged: not per-repository)"),
    ("gho_", "github oauth"),
    ("ghs_", "github app installation"),
    ("glpat-", "gitlab personal"),
    ("gldt-", "gitlab deploy"),
)

MATCH_RE = re.compile(r"^[A-Za-z0-9_.-]+/([A-Za-z0-9_.-]+|\*)$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SCOPES = ("read", "write")

MIN_TOKEN_LENGTH = 20
MAX_TOKEN_LENGTH = 512


class GitCredError(Exception):
    """A caller-fixable problem (reported, not raised)."""


# --------------------------------------------------------------------- store


def cred_dir(tenant: Tenant) -> Path:
    return config.TENANTS_DIR / f"{tenant.name}.git"


def _index_path(tenant: Tenant) -> Path:
    return cred_dir(tenant) / "index.json"


def _token_path(tenant: Tenant, cred_id: str) -> Path:
    return cred_dir(tenant) / f"{cred_id}.token"


@contextmanager
def _locked(tenant: Tenant):
    """Serialise read-modify-write of the index across processes."""
    directory = cred_dir(tenant)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd = os.open(directory / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_index(tenant: Tenant) -> list[dict]:
    try:
        data = json.loads(_index_path(tenant).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_index(tenant: Tenant, entries: list[dict]) -> None:
    path = _index_path(tenant)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(entries, indent=2) + "\n").encode())
    finally:
        os.close(fd)
    os.replace(temp, path)


# ---------------------------------------------------------------- validation


def validate_host(host: str) -> str:
    host = (host or "").strip().lower()
    if host not in ALLOWED_HOSTS:
        raise GitCredError(
            f"host {host!r} is not allowed; this service talks to "
            f"{', '.join(ALLOWED_HOSTS)}"
        )
    return host


def validate_match(match: str, *, allow_wildcard: bool = True) -> str:
    match = (match or "").strip()
    if not MATCH_RE.match(match):
        raise GitCredError(
            f"invalid match {match!r}: expected owner/name"
            + (" or owner/*" if allow_wildcard else "")
        )
    # A dot is legal in repository names (`owner/.github` is a real one), so
    # the character class allows it — but a component that is only dots is a
    # path-traversal shape and matches no repository that exists.
    if any(part in (".", "..") for part in match.split("/")):
        raise GitCredError(f"invalid match {match!r}: '.' and '..' are not names")
    if not allow_wildcard and match.endswith("/*"):
        raise GitCredError("a wildcard match is not allowed here")
    return match


def validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise GitCredError(f"scope must be one of {', '.join(SCOPES)}")
    return scope


def describe_token(token: str) -> str:
    """The token's kind, from its prefix. Never returns or logs the value."""
    if not isinstance(token, str) or not token.strip():
        raise GitCredError("no token given")
    token = token.strip()
    if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
        # Deliberately does not say how long it was: the length of a secret is
        # itself a hint.
        raise GitCredError("that does not look like a forge token")
    if any(c.isspace() for c in token):
        raise GitCredError("a token must not contain whitespace")
    for prefix, description in TOKEN_PREFIXES:
        if token.startswith(prefix):
            return description
    raise GitCredError(
        "unrecognised token prefix; expected one of: "
        + ", ".join(p for p, _ in TOKEN_PREFIXES)
    )


# ------------------------------------------------------------------- records


def new_id(host: str, match: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{host}-{match}".lower()).strip("-")
    return f"{slug[:48]}-{os.urandom(3).hex()}"


def add_token(tenant: Tenant, host: str, match: str, token: str,
              scope: str = "read", owner: tuple[int, int] | None = None) -> dict:
    """Store a token and return its metadata. The value is never returned."""
    host = validate_host(host)
    match = validate_match(match)
    scope = validate_scope(scope)
    description = describe_token(token)

    with _locked(tenant):
        entries = _read_index(tenant)
        cred_id = new_id(host, match)
        path = _token_path(tenant, cred_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.strip().encode())
        finally:
            os.close(fd)
        if owner:
            os.chown(path, *owner)
        entry = {
            "id": cred_id, "host": host, "match": match, "kind": "token",
            "scope": scope, "token_kind": description,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        # Same (host, match, scope) replaces its predecessor: re-registering
        # after a rotation should not leave the old token on disk.
        for old in [e for e in entries
                    if (e.get("host"), e.get("match"), e.get("scope"))
                    == (host, match, scope)]:
            _token_path(tenant, old["id"]).unlink(missing_ok=True)
            entries.remove(old)
        entries.append(entry)
        _write_index(tenant, entries)
        if owner:
            os.chown(_index_path(tenant), *owner)
            os.chown(cred_dir(tenant), *owner)
            os.chown(cred_dir(tenant) / ".lock", *owner)
    return entry


def list_credentials(tenant: Tenant) -> list[dict]:
    """Metadata only. There is no code path that returns a token value."""
    return [
        {k: v for k, v in entry.items() if k != "token"}
        for entry in _read_index(tenant)
    ]


def remove(tenant: Tenant, cred_id: str) -> bool:
    with _locked(tenant):
        entries = _read_index(tenant)
        kept = [e for e in entries if e.get("id") != cred_id]
        if len(kept) == len(entries):
            return False
        _token_path(tenant, cred_id).unlink(missing_ok=True)
        _write_index(tenant, kept)
    return True


# ----------------------------------------------------------------- selection


def parse_url(url: str) -> tuple[str, str]:
    """``(host, owner/name)`` from an https git URL, or raise.

    Only https is accepted. An ssh URL, a local path, `ext::` and a URL
    carrying userinfo are all refused — the last because a URL with a secret
    in it is exactly what this module exists to avoid.
    """
    if not isinstance(url, str) or not url.strip():
        raise GitCredError("no repository URL given")
    url = url.strip()
    if not url.startswith("https://"):
        raise GitCredError(
            "only https:// repository URLs are accepted "
            "(ssh and local transports are refused)"
        )
    parts = urlsplit(url)
    if parts.username or parts.password or "@" in parts.netloc:
        raise GitCredError(
            "the URL must not contain credentials; register one instead"
        )
    host = validate_host(parts.hostname or "")
    path = parts.path.strip("/")
    path = path.removesuffix(".git")
    if ".." in path.split("/"):
        raise GitCredError(f"invalid repository path {parts.path!r}")
    if not REPO_RE.match(path):
        raise GitCredError(
            f"expected https://{host}/owner/name, got {parts.path!r}"
        )
    return host, path


def select(tenant: Tenant, host: str, repo: str,
           scope: str = "read") -> dict | None:
    """The credential to use, or None for anonymous access.

    The most specific match wins, and a credential is never offered to a host
    other than its own. A ``write`` credential is never returned for a
    ``read`` request: a caller that only needs to read must not silently get
    the ability to push.
    """
    owner = repo.split("/")[0]
    candidates = [
        e for e in _read_index(tenant)
        if e.get("host") == host
        and e.get("scope", "read") == scope
        and e.get("match") in (repo, f"{owner}/*")
    ]
    if not candidates:
        return None
    # owner/name beats owner/*
    candidates.sort(key=lambda e: 0 if e.get("match") == repo else 1)
    return candidates[0]


def read_token(tenant: Tenant, cred_id: str) -> str:
    try:
        return _token_path(tenant, cred_id).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GitCredError(f"credential {cred_id!r} is unreadable: {exc}") from None


# --------------------------------------------------------------- git plumbing


def git_env(tenant: Tenant, entry: dict | None, base: dict | None = None) -> dict:
    """The environment for one git invocation, carrying the credential.

    The token goes into ``GIT_CONFIG_VALUE_0`` as an ``Authorization`` header
    for that host only. Not argv, because argv is world-readable in ``ps``;
    not the URL, because the URL is written into ``.git/config``.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tenant.workspace),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ASKPASS": "/bin/true",
        **(base or {}),
    }
    if entry is None:
        return env
    token = read_token(tenant, entry["id"])
    user = TOKEN_USERS.get(entry["host"], "oauth2")
    basic = base64.b64encode(f"{user}:{token}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"http.https://{entry['host']}/.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return env


# ------------------------------------------------------------------- mirrors


def mirror_path(tenant: Tenant, host: str, repo: str) -> Path:
    owner, name = repo.split("/", 1)
    return (config.TENANTS_DIR / f"{tenant.name}.mirrors" / host / owner
            / f"{name}.git")


_ORIGIN_RE = re.compile(
    r'\[remote\s+"origin"\]([^\[]*)', re.IGNORECASE | re.DOTALL)
_URL_RE = re.compile(r"^\s*url\s*=\s*(.+?)\s*$", re.MULTILINE)


def origin_url(repo_dir: Path) -> str:
    """``remote.origin.url`` read as **text**, never via git.

    `git -C <repo> config` would interpret that repository's own
    configuration, and a repository in a workspace is written by an agent:
    `include.path` alone can make git read files it should not. Reading the
    file and matching two patterns cannot execute or include anything.
    """
    try:
        text = (repo_dir / ".git" / "config").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        raise GitCredError(
            f"{repo_dir.name!r} is not a git repository (no .git/config)"
        ) from None
    section = _ORIGIN_RE.search(text)
    if not section:
        raise GitCredError(f"{repo_dir.name!r} has no 'origin' remote")
    found = _URL_RE.search(section.group(1))
    if not found:
        raise GitCredError(f"{repo_dir.name!r} has no url for 'origin'")
    return found.group(1).strip()


async def update_mirror(tenant: Tenant, host: str, repo: str, url: str,
                        entry: dict | None) -> Path:
    """Fetch into the tenant's host-owned bare mirror, with the credential.

    The mirror exists so the credential never has to be used inside a
    repository an agent can write: its config is the service's own.
    """
    import asyncio

    path = mirror_path(tenant, host, repo)
    env = git_env(tenant, entry)
    if (path / "HEAD").exists():
        argv = ["git", "-C", str(path), "-c", "protocol.allow=never",
                "-c", "protocol.https.allow=always",
                "fetch", "--prune", "--no-tags", "origin",
                "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(config.TENANTS_DIR / f"{tenant.name}.mirrors", 0o700)
        argv = ["git", "-c", "protocol.allow=never",
                "-c", "protocol.https.allow=always",
                "-c", "core.hooksPath=/dev/null",
                "clone", "--mirror", "--", url, str(path)]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(),
                                        timeout=config.CLONE_TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise GitCredError(
            f"the mirror fetch timed out after {config.CLONE_TIMEOUT}s"
        ) from None
    if proc.returncode != 0:
        # git's stderr can echo the URL; it never contains the auth header.
        raise GitCredError(
            f"the mirror fetch failed: "
            f"{err.decode(errors='replace').strip()[-400:]}"
        )
    return path


def safe_log_fields(entry: dict | None) -> dict:
    """What may be logged about a credential: its id and shape, never its value."""
    if entry is None:
        return {"credential": "anonymous"}
    return {"credential": entry.get("id"), "scope": entry.get("scope"),
            "token_kind": entry.get("token_kind")}
