# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The service's own nginx password file — never the fleet-wide one.

``/etc/nginx/.htpasswd-mcp`` is applied at server level and grants every fleet
MCP, so a login provisioned here must not go in it. This module owns
``/var/lib/mcp-agentbridge/htpasswd/users``, which the agentbridge location
overrides with, and which lives under ``/var/lib`` because the unit's
``ProtectSystem=full`` makes ``/etc`` read-only.

Passwords are stored only as bcrypt. Writes are atomic and serialised with a
lock file, because the service and ``agentbridge-admin`` can both write here:
a half-written file would lock every tenant out at once.

nginx re-reads the file per request, so no reload is ever needed.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import string
from contextlib import contextmanager
from pathlib import Path

import bcrypt

from . import config
from .tenant import TenantError, validate_username

MIN_PASSWORD_LENGTH = 12
# Alphanumeric only: the value is typed into a connector dialog and travels in
# an HTTP header, so punctuation buys nothing and invites quoting mistakes.
_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = 24) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def hash_password(password: str) -> str:
    """bcrypt, written with the ``$2y$`` prefix that every nginx build accepts
    (``$2b$`` and ``$2y$`` are the same format; libxcrypt treats them alike)."""
    digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    if digest.startswith("$2b$"):
        return "$2y$" + digest.split("$", 2)[2]
    return digest


def _path() -> Path:
    return config.HTPASSWD_FILE


@contextmanager
def _locked(path: Path):
    """Hold an exclusive lock for a read-modify-write of the password file."""
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    lock = path.parent / ".lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_lines(path: Path) -> list[str]:
    try:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def _write_lines(path: Path, lines: list[str]) -> None:
    """Replace the file atomically, keeping its mode, owner group and a backup.

    The temp file is created in the same directory so ``os.replace`` cannot
    cross a filesystem, and is removed if anything fails — a stray temp file
    in a directory nginx reads would be noise at best.
    """
    try:
        st = path.stat()
        mode, uid, gid = st.st_mode & 0o7777, st.st_uid, st.st_gid
    except FileNotFoundError:
        parent = path.parent.stat()
        mode, uid, gid = 0o640, -1, parent.st_gid

    tmp = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(f"{ln}\n" for ln in lines))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if os.geteuid() == 0 or gid in os.getgroups():
            os.chown(tmp, uid, gid)
        # Otherwise the group is whatever the directory gives us. That is why
        # the directory is setgid to nginx's group (see
        # admin._fix_htpasswd_ownership): a non-root process cannot chgrp to a
        # group it does not belong to, and a file nginx cannot read turns
        # every authenticated request into a 500.
        if path.exists():
            # Keep one generation back: recovering a lost line beats recreating
            # every tenant's password.
            backup = path.with_name(f"{path.name}.bak")
            os.replace(path, backup)
            os.chmod(backup, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def set_entry(name: str, password: str, *, path: Path | None = None) -> None:
    """Add or replace one login's line. Other lines stay byte-for-byte."""
    validate_username(name)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise TenantError(
            f"password too short: at least {MIN_PASSWORD_LENGTH} characters"
        )
    path = path or _path()
    line = f"{name}:{hash_password(password)}"
    with _locked(path):
        lines = [ln for ln in _read_lines(path) if not _is_entry(ln, name)]
        lines.append(line)
        _write_lines(path, sorted(lines))


def remove_entry(name: str, *, path: Path | None = None) -> bool:
    """Drop one login's line. Returns whether there was one."""
    validate_username(name)
    path = path or _path()
    with _locked(path):
        lines = _read_lines(path)
        kept = [ln for ln in lines if not _is_entry(ln, name)]
        if len(kept) == len(lines):
            return False
        _write_lines(path, kept)
        return True


def has_entry(name: str, *, path: Path | None = None) -> bool:
    validate_username(name)
    return any(_is_entry(ln, name) for ln in _read_lines(path or _path()))


def entries(*, path: Path | None = None) -> list[str]:
    """Login names only. The hashes are never returned anywhere."""
    return sorted(
        ln.split(":", 1)[0] for ln in _read_lines(path or _path()) if ":" in ln
    )


def _is_entry(line: str, name: str) -> bool:
    return line.split(":", 1)[0] == name
