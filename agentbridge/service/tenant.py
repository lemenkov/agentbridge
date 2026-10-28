# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Caller → tenant resolution.

The tenant is derived only from the reverse proxy's user header, never from a
tool argument, and there is no default identity: a missing header, a malformed
name or an unprovisioned user is a ``TenantError`` naming what is missing.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import config

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Credential secret kinds, in order of preference, with the env var claude reads.
CREDENTIAL_KINDS = (
    ("apikey", "ANTHROPIC_API_KEY"),
    ("oauth", "CLAUDE_CODE_OAUTH_TOKEN"),
)

# A credential's kind is decided by its prefix, and nothing else: guessing
# ("not an API key, so presumably OAuth") would store a typo under a name the
# worker then exports as a token, and the failure would surface much later.
CREDENTIAL_PREFIXES = (
    ("sk-ant-api", "apikey"),
    ("sk-ant-oat", "oauth"),
)


class TenantError(Exception):
    """A caller-fixable problem resolving the tenant (reported, not raised)."""


@dataclass(frozen=True)
class Tenant:
    name: str
    vhost: str
    bus_user: str
    bus_password: str
    max_workers: int
    # Admin tools are refused unless this is set in the tenant record. nginx
    # cannot tell admins apart — it only knows logins — so the service decides.
    admin: bool = False

    @property
    def amqp_url(self) -> str:
        return (f"amqp://{quote(self.bus_user, safe='')}:"
                f"{quote(self.bus_password, safe='')}@{config.AMQP_HOST}/"
                f"{quote(self.vhost, safe='')}")

    @property
    def amqp_url_file(self) -> Path:
        return config.TENANTS_DIR / f"{self.name}.amqp"

    @property
    def gitconfig(self) -> Path:
        return config.TENANTS_DIR / f"{self.name}.gitconfig"

    @property
    def ledger(self) -> Path:
        """Per-task metadata, deliberately beside the tenant records rather
        than in the workspace: a read-write agent must not be able to edit the
        record of what it did."""
        return config.TENANTS_DIR / f"{self.name}.ledger.jsonl"

    @property
    def checks_dir(self) -> Path:
        """Check logs and results. Outside the workspace on purpose: an
        implementer can write its workspace, and a check log the subject of
        the check can rewrite proves nothing."""
        return config.TENANTS_DIR / f"{self.name}.checks"

    @property
    def status_dir(self) -> Path:
        """One status file per worker, written by its bridge."""
        return config.TENANTS_DIR / f"{self.name}.status"

    @property
    def workspace(self) -> Path:
        return config.WORKSPACES_DIR / self.name

    @property
    def unit_prefix(self) -> str:
        return f"agentbridge-{self.name}-"

    def secret_name(self, kind: str) -> str:
        return f"anthropic-{self.name}-{kind}"


def validate_username(name: str) -> str:
    if not USERNAME_RE.match(name or ""):
        raise TenantError(
            f"invalid user name {name!r}: must match {USERNAME_RE.pattern}"
        )
    return name


def load_tenant(name: str) -> Tenant:
    validate_username(name)
    path = config.TENANTS_DIR / f"{name}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TenantError(
            f"user {name!r} is not provisioned on this service — ask the "
            f"operator to run `agentbridge-admin provision {name}`"
        ) from None
    return Tenant(
        name=name,
        vhost=record["vhost"],
        bus_user=record["bus_user"],
        bus_password=record["bus_password"],
        max_workers=int(record.get("limits", {}).get(
            "max_workers", config.DEFAULT_MAX_WORKERS)),
        admin=bool(record.get("admin", False)),
    )


def tenant_from_headers(headers: dict) -> Tenant:
    """Resolve the calling tenant from request headers (keys are lowercase)."""
    name = (headers or {}).get(config.USER_HEADER, "").strip()
    if not name:
        raise TenantError(
            f"no authenticated user: the {config.USER_HEADER!r} header is "
            f"missing (the service must be reached through the reverse proxy)"
        )
    return load_tenant(name)


async def podman_secret_names() -> set[str]:
    """Names of the podman secrets in the service user's store (names only)."""
    proc = await asyncio.create_subprocess_exec(
        "podman", "secret", "ls", "--format", "{{.Name}}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"podman secret ls failed: {err.decode().strip()}")
    return set(out.decode().split())


def credential_kind(token: str) -> str:
    """``apikey`` or ``oauth``, from the token's prefix.

    The error names the prefixes but never the value, not even its length:
    length alone distinguishes the kinds.
    """
    for prefix, kind in CREDENTIAL_PREFIXES:
        if (token or "").startswith(prefix):
            return kind
    raise TenantError(
        "unrecognised credential: expected an Anthropic API key "
        f"({CREDENTIAL_PREFIXES[0][0]}…) or an OAuth token "
        f"({CREDENTIAL_PREFIXES[1][0]}…); nothing was stored"
    )


def pick_credential(tenant: Tenant, secret_names: set[str]) -> tuple[str, str] | None:
    """``(secret name, env var)`` for the tenant's credential; apikey wins."""
    for kind, env_var in CREDENTIAL_KINDS:
        name = tenant.secret_name(kind)
        if name in secret_names:
            return name, env_var
    return None
