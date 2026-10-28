# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The per-tenant gitconfig mounted into read-write leaves.

The file is a config language, so a name or email is not just text: a newline
in either would let the caller append whole sections. ``core.fsmonitor``,
``core.sshCommand``, a filter driver or ``include.path`` all name a command or
a file that git then runs or reads, so a caller that can inject sections can
choose what runs wherever this file is used. Hence: validate first, then quote.
"""

from __future__ import annotations

import fcntl
import os
import re
from pathlib import Path

from .tenant import Tenant, TenantError

MAX_NAME = 100
MAX_EMAIL = 254

# No control characters at all, printable only, and not blank.
_NAME_RE = re.compile(rf"^[^\x00-\x1f\x7f]{{1,{MAX_NAME}}}$")
# Deliberately strict rather than RFC-complete: one @, no spaces, a dot in the
# domain. A committer identity that cannot be expressed here can be set by the
# operator in the file directly.
_EMAIL_RE = re.compile(r"^[^\x00-\x20\x7f@,<>\"']+@[^\x00-\x20\x7f@,<>\"']+"
                       r"\.[^\x00-\x20\x7f@,<>\"'.]+$")


def _quote(value: str) -> str:
    """git's quoted form for a config value: escape backslash and quote."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_identity(name: str, email: str) -> tuple[str, str]:
    name = (name or "").strip()
    email = (email or "").strip()
    if not _NAME_RE.match(name):
        raise TenantError(
            f"invalid committer name: printable text, 1–{MAX_NAME} characters, "
            "no line breaks"
        )
    if len(email) > MAX_EMAIL or not _EMAIL_RE.match(email):
        raise TenantError(
            "invalid committer email: expected something like "
            "name@example.com, with no spaces or line breaks"
        )
    return name, email


def render(name: str, email: str) -> str:
    """The file's contents. ``safe.directory`` is kept: the workspace is owned
    by the service user, not by root inside the container, and git refuses a
    repository it considers someone else's."""
    return (
        "[user]\n"
        f"\tname = {_quote(name)}\n"
        f"\temail = {_quote(email)}\n"
        "[safe]\n"
        "\tdirectory = *\n"
    )


def write_identity(tenant: Tenant, name: str, email: str) -> dict:
    """Replace the tenant's gitconfig atomically. Returns what was stored."""
    name, email = validate_identity(name, email)
    path = tenant.gitconfig
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    lock = path.with_suffix(".gitconfig.lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(out, "w", encoding="utf-8") as fh:
                fh.write(render(name, email))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    finally:
        os.close(fd)
    return {"name": name, "email": email, "path": str(path)}


def read_identity(tenant: Tenant) -> dict | None:
    """The stored identity, or ``None`` if the tenant has none."""
    try:
        text = tenant.gitconfig.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    found = {}
    for key in ("name", "email"):
        match = re.search(rf"^\s*{key}\s*=\s*(.+?)\s*$", text, re.MULTILINE)
        if match:
            value = match.group(1)
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            found[key] = value
    return found or None
