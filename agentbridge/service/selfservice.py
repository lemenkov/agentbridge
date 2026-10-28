# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""What a tenant can change about itself, without the operator.

Each function is the whole of one tool's behaviour, so the tools in
``server.py`` stay thin and these stay testable. Every one of them is reached
only after the caller's tenant has been resolved from the proxy header, so
none of them takes a user name: a tenant can only ever act on itself.

Blocking work (bcrypt, podman, fsync) runs in a thread: the service is a
single event loop serving every tenant, and a 100 ms hash would stall the lot.
"""

from __future__ import annotations

import asyncio

from . import gitconfig, htpasswd
from . import secrets as secrets_mod
from .tenant import CREDENTIAL_KINDS, Tenant, TenantError, credential_kind


async def register_credential(tenant: Tenant, token: str) -> dict:
    """Store the caller's Anthropic credential as their podman secret.

    The value is never logged, echoed, measured or written to a file: it goes
    straight to podman's stdin and is dropped.
    """
    kind = credential_kind(token)
    try:
        await asyncio.to_thread(
            secrets_mod.create, tenant.secret_name(kind), token
        )
    except secrets_mod.SecretError as e:
        raise TenantError(str(e)) from None
    return {
        "registered": kind,
        "secret": tenant.secret_name(kind),
        "note": "new workers use it; existing workers keep the old one until retired",
    }


async def revoke_credential(tenant: Tenant, kind: str) -> dict:
    """Remove one of the caller's credentials. Absent is not an error."""
    kinds = [k for k, _ in CREDENTIAL_KINDS]
    if kind not in kinds:
        raise TenantError(f"unknown credential kind {kind!r}: one of {', '.join(kinds)}")
    try:
        existed = await asyncio.to_thread(
            secrets_mod.remove, tenant.secret_name(kind)
        )
    except secrets_mod.SecretError as e:
        raise TenantError(str(e)) from None
    return {"revoked": kind, "existed": existed}


async def set_password(tenant: Tenant, new_password: str) -> dict:
    """Rewrite the caller's own line in the service's password file."""
    await asyncio.to_thread(htpasswd.set_entry, tenant.name, new_password)
    return {
        "updated": tenant.name,
        "note": "nginx re-reads the file per request; update the connector now",
    }


async def set_git_identity(tenant: Tenant, name: str, email: str) -> dict:
    """Set the committer identity that read-write agents commit with."""
    stored = await asyncio.to_thread(gitconfig.write_identity, tenant, name, email)
    return {"name": stored["name"], "email": stored["email"]}
