# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The tenant's workspace directory, seen from the host. Every caller-supplied
path is resolved strictly inside it: absolute paths and ``..`` are refused, and
symlinks are resolved before the containment check, since implementer agents
can write arbitrary symlinks into a read-write workspace."""

import asyncio
import os
import secrets
from pathlib import Path

from . import config
from .tenant import Tenant

OUTBOX = ".outbox"


class WorkspaceError(Exception):
    """A caller-fixable path problem (reported, not raised)."""


def resolve(tenant: Tenant, path: str) -> Path:
    root = tenant.workspace.resolve()
    rel = Path(path or ".")
    if rel.is_absolute():
        raise WorkspaceError(f"path {path!r} must be relative to the workspace")
    if ".." in rel.parts:
        raise WorkspaceError(f"path {path!r} must not contain '..'")
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise WorkspaceError(f"path {path!r} resolves outside the workspace")
    return target


def ls(tenant: Tenant, path: str) -> list[dict]:
    target = resolve(tenant, path)
    if not target.is_dir():
        raise WorkspaceError(f"{path!r} is not a directory")
    entries = []
    for entry in sorted(target.iterdir()):
        st = entry.lstat()
        kind = "link" if entry.is_symlink() else "dir" if entry.is_dir() else "file"
        entries.append({"name": entry.name, "type": kind, "bytes": st.st_size})
    return entries


def read(tenant: Tenant, path: str, max_bytes: int) -> dict:
    target = resolve(tenant, path)
    if not target.is_file():
        raise WorkspaceError(f"{path!r} is not a file")
    size = target.stat().st_size
    with open(target, "rb") as fh:
        data = fh.read(max(0, min(max_bytes, config.WORKSPACE_READ_MAX)))
    return {
        "path": path,
        "bytes": size,
        "truncated": len(data) < size,
        "text": data.decode("utf-8", errors="replace"),
    }


def write(tenant: Tenant, path: str, text: str) -> dict:
    target = resolve(tenant, path)
    if target == tenant.workspace.resolve():
        raise WorkspaceError("cannot write to the workspace root itself")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    target.write_bytes(data)
    return {"path": path, "bytes": len(data)}


def outbox_put(tenant: Tenant, token: str, index: int, text: str) -> str:
    """Store a large result in the outbox; return its workspace-relative path."""
    rel = f"{OUTBOX}/{token}/{index}.txt"
    target = tenant.workspace / OUTBOX / token
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{index}.txt").write_text(text, encoding="utf-8")
    return rel


def new_outbox_token() -> str:
    return secrets.token_hex(8)


async def clone(tenant: Tenant, url: str, dest: str) -> dict:
    """``git clone`` an https repository into the workspace, on the host."""
    if not url.startswith("https://"):
        raise WorkspaceError("only https:// repository URLs are accepted")
    target = resolve(tenant, dest)
    if target.exists():
        raise WorkspaceError(f"destination {dest!r} already exists")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tenant.workspace),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
        "-c", "core.hooksPath=/dev/null",
        "clone", "--no-recurse-submodules", "--", url, str(target),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=config.CLONE_TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise WorkspaceError(f"git clone timed out after {config.CLONE_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise WorkspaceError(f"git clone failed: {err.decode(errors='replace').strip()[-500:]}")
    return {"cloned": url, "dest": dest}
