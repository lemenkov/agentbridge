# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Creating and removing tenants, as one library with two callers.

``agentbridge-admin`` (root) and the admin endpoint (the service user) must do
exactly the same thing, or the two paths drift and the CLI becomes the only
one that really works. The differences between them are narrow and passed in:

* **owner** — root has to hand new files to the service user; the service
  already owns them.
* **secret_runner** — root reaches the service user's podman store through
  ``runuser``; the service talks to it directly.

Stopping a tenant's workers is *not* here: root does it with ``runuser`` and
the service with its own ``workers`` module, and neither belongs in a sync
library. The caller does it first, then calls ``deprovision``.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import shutil
import time
from pathlib import Path

from . import config, htpasswd, rabbit_mgmt
from . import secrets as secrets_mod
from .tenant import (
    CREDENTIAL_KINDS,
    Tenant,
    TenantError,
    load_tenant,
    validate_username,
)

Owner = tuple[int, int] | None


def _write_private(path: Path, text: str, owner: Owner = None) -> None:
    """0600 from the moment it exists — the record holds the bus password."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    if owner:
        os.chown(path, *owner)


def _ensure_dir(path: Path, mode: int, owner: Owner = None) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if owner:
        os.chown(path, *owner)


def provision(name: str, *, max_workers: int | None = None,
              admin: bool | None = None, reset_password: bool = False,
              owner: Owner = None) -> dict:
    """Create or update a tenant. Idempotent, and safe to re-run.

    Returns the tenant's facts plus ``password``, which is a freshly generated
    login password when one was issued and ``None`` when the existing login
    was left alone. It is the only time that value exists outside bcrypt, so
    the caller must show it once and keep no copy.
    """
    name = validate_username(name)
    record_path = config.TENANTS_DIR / f"{name}.json"

    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if max_workers is not None:
            record.setdefault("limits", {})["max_workers"] = max_workers
        if admin is not None:
            record["admin"] = admin
    else:
        record = {
            "vhost": f"ab-{name}",
            "bus_user": f"ab-{name}",
            "bus_password": _secrets.token_hex(24),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "limits": {"max_workers": max_workers or config.DEFAULT_MAX_WORKERS},
            "admin": bool(admin),
        }
    tenant = Tenant(
        name=name, vhost=record["vhost"], bus_user=record["bus_user"],
        bus_password=record["bus_password"],
        max_workers=int(record["limits"]["max_workers"]),
        admin=bool(record.get("admin", False)),
    )

    # Broker first: if it fails we have written nothing, so a retry is clean.
    rabbit_mgmt.ensure_tenant(tenant.vhost, tenant.bus_user, tenant.bus_password)

    _ensure_dir(config.TENANTS_DIR, 0o700, owner)
    _write_private(record_path, json.dumps(record, indent=2) + "\n", owner)
    _write_private(tenant.amqp_url_file, tenant.amqp_url + "\n", owner)
    _ensure_dir(tenant.workspace, 0o700, owner)
    # Workers write one status file each in here (fleet_status reads them).
    _ensure_dir(tenant.status_dir, 0o700, owner)
    # Check logs and results (run_check), likewise outside the workspace.
    _ensure_dir(tenant.checks_dir, 0o700, owner)

    password = None
    if reset_password or not htpasswd.has_entry(name):
        password = htpasswd.generate_password()
        htpasswd.set_entry(name, password)

    return {
        "user": tenant.name,
        "vhost": tenant.vhost,
        "bus_user": tenant.bus_user,
        "workspace": str(tenant.workspace),
        "max_workers": tenant.max_workers,
        "admin": tenant.admin,
        "password": password,
        "has_git_identity": tenant.gitconfig.exists(),
    }


def deprovision(name: str, *, purge: bool = False,
                secret_runner=secrets_mod.direct_runner) -> dict:
    """Remove a tenant's broker objects, login and records.

    The caller stops the workers first. ``purge`` additionally destroys the
    workspace and the credential secrets, which is not reversible.
    """
    tenant = load_tenant(name)
    rabbit_mgmt.delete_vhost(tenant.vhost)
    rabbit_mgmt.delete_user(tenant.bus_user)
    had_login = htpasswd.remove_entry(tenant.name)

    for path in (config.TENANTS_DIR / f"{tenant.name}.json", tenant.amqp_url_file,
                 tenant.gitconfig, tenant.gitconfig.with_suffix(".gitconfig.lock")):
        path.unlink(missing_ok=True)
    # Status files and the ledger describe a tenant that no longer exists.
    # The ledger goes only on purge: it is the record of what was spent.
    shutil.rmtree(tenant.status_dir, ignore_errors=True)
    shutil.rmtree(tenant.checks_dir, ignore_errors=True)

    purged = []
    if purge:
        shutil.rmtree(tenant.workspace, ignore_errors=True)
        purged.append(str(tenant.workspace))
        for path in (tenant.ledger, tenant.ledger.with_name(tenant.ledger.name + ".1"),
                     tenant.ledger.with_name(tenant.ledger.name + ".lock")):
            if path.exists():
                path.unlink(missing_ok=True)
                purged.append(str(path))
        for kind, _ in CREDENTIAL_KINDS:
            if secrets_mod.remove(tenant.secret_name(kind), runner=secret_runner):
                purged.append(tenant.secret_name(kind))
    return {
        "user": tenant.name,
        "vhost_deleted": tenant.vhost,
        "login_removed": had_login,
        "purged": purged,
        "workspace_kept": None if purge else str(tenant.workspace),
    }


def list_tenants(*, secret_names: set[str] | None = None,
                 unit_names: list[str] | None = None) -> list[dict]:
    """Every tenant, with credential *kinds* (never values) and worker counts."""
    secret_names = secret_names or set()
    unit_names = unit_names or []
    logins = set(htpasswd.entries())
    out = []
    for path in sorted(config.TENANTS_DIR.glob("*.json")):
        name = path.stem
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "user": name,
            "admin": bool(record.get("admin", False)),
            "max_workers": record.get("limits", {}).get("max_workers"),
            "created": record.get("created"),
            "credentials": [kind for kind, _ in CREDENTIAL_KINDS
                            if f"anthropic-{name}-{kind}" in secret_names],
            "login": name in logins,
            "workers": sum(1 for u in unit_names
                           if u.startswith(f"agentbridge-{name}-")),
        })
    return out


def require_not_self(caller: str, target: str) -> None:
    """Guard against an admin removing the account they are calling with."""
    if caller == target:
        raise TenantError(
            f"refusing to deprovision {target!r}: that is the account you are "
            "calling with. Ask another admin, or use agentbridge-admin as root."
        )
