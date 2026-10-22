# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""agentbridge-admin: provision, list and deprovision tenants. Run as root.

    agentbridge-admin provision petro --git-name "..." --git-email "..."
    agentbridge-admin list
    agentbridge-admin deprovision petro [--purge]

The bus password is service plumbing: generated here, handed to rabbitmqctl
only as a salted hash, stored 0600 for the service user, and never shown. A user's only secret
is their Anthropic credential, which the operator registers out of band as a
podman secret of the service user (printed as the last provisioning step).
"""

import argparse
import base64
import hashlib
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

SYSCONFIG = Path("/etc/sysconfig/mcp-agentbridge")


def _load_sysconfig() -> None:
    """Pick up the service's paths so admin and service always agree."""
    if not SYSCONFIG.exists():
        return
    for line in SYSCONFIG.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def _service_user() -> pwd.struct_passwd:
    name = os.environ.get("SERVICE_USER", "agentbridge")
    try:
        return pwd.getpwnam(name)
    except KeyError:
        sys.exit(f"service user {name!r} does not exist — run deploy/setup-host.sh first")


def _rabbitmqctl(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["rabbitmqctl", "--quiet", *args], stdin=subprocess.DEVNULL, text=True,
        capture_output=True, check=False,
    )
    if check and proc.returncode != 0:
        sys.exit(f"rabbitmqctl {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def _rabbit_password_hash(password: str) -> str:
    """RabbitMQ's default (rabbit_password_hashing_sha256) stored form:
    base64(salt || sha256(salt || password)) with a 4-byte random salt. Passing
    this to `add_user --pre-hashed-password` keeps the password out of argv."""
    salt = secrets.token_bytes(4)
    return base64.b64encode(salt + hashlib.sha256(salt + password.encode()).digest()).decode()


def _as_service_user(svc: pwd.struct_passwd, *argv: str) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": svc.pw_dir,
           "XDG_RUNTIME_DIR": f"/run/user/{svc.pw_uid}"}
    return subprocess.run(["runuser", "-u", svc.pw_name, "--", "env",
                           *(f"{k}={v}" for k, v in env.items()), *argv],
                          text=True, capture_output=True, check=False)


def _write_private(path: Path, text: str, svc: pwd.struct_passwd) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    os.chown(path, svc.pw_uid, svc.pw_gid)


def cmd_provision(args: argparse.Namespace) -> None:
    from . import config
    from .tenant import Tenant, TenantError, validate_username

    try:
        name = validate_username(args.user)
    except TenantError as e:
        sys.exit(str(e))
    svc = _service_user()
    record_path = config.TENANTS_DIR / f"{name}.json"

    if record_path.exists():
        record = json.loads(record_path.read_text())
        if args.max_workers is not None:
            record.setdefault("limits", {})["max_workers"] = args.max_workers
    else:
        record = {
            "vhost": f"ab-{name}",
            "bus_user": f"ab-{name}",
            "bus_password": secrets.token_hex(24),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "limits": {"max_workers": args.max_workers or config.DEFAULT_MAX_WORKERS},
        }
    tenant = Tenant(name=name, vhost=record["vhost"], bus_user=record["bus_user"],
                    bus_password=record["bus_password"],
                    max_workers=record["limits"]["max_workers"])

    # Broker: vhost + a user permitted on that vhost only. Only a salted hash of
    # the password reaches argv. change_password has no pre-hashed form, so
    # re-running re-creates the user to resync it to the record.
    if tenant.vhost not in _rabbitmqctl("list_vhosts", "name").split():
        _rabbitmqctl("add_vhost", tenant.vhost)
    users = [line.split()[0] for line in
             _rabbitmqctl("list_users", "--no-table-headers").splitlines() if line.strip()]
    if tenant.bus_user in users:
        _rabbitmqctl("delete_user", tenant.bus_user)
    _rabbitmqctl("add_user", tenant.bus_user,
                 _rabbit_password_hash(tenant.bus_password), "--pre-hashed-password")
    _rabbitmqctl("set_permissions", "-p", tenant.vhost, tenant.bus_user,
                 ".*", ".*", ".*")

    # Files: tenant record + bus URL file (0600, service user), workspace (0700).
    config.TENANTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(config.TENANTS_DIR, svc.pw_uid, svc.pw_gid)
    _write_private(record_path, json.dumps(record, indent=2) + "\n", svc)
    _write_private(tenant.amqp_url_file, tenant.amqp_url + "\n", svc)
    if args.git_name and args.git_email:
        _write_private(tenant.gitconfig,
                       f"[user]\n\tname = {args.git_name}\n\temail = {args.git_email}\n"
                       "[safe]\n\tdirectory = *\n", svc)
    tenant.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(tenant.workspace, svc.pw_uid, svc.pw_gid)

    print(f"provisioned {name}: vhost {tenant.vhost}, bus user {tenant.bus_user}, "
          f"workspace {tenant.workspace}, max_workers {tenant.max_workers}")
    if not tenant.gitconfig.exists():
        print("  note: no committer identity (--git-name/--git-email); "
              "implementers will not be able to commit")
    print("Remaining manual steps:")
    print(f"  htpasswd /etc/nginx/.htpasswd-mcp {name}        # if not already there")
    print(f"  runuser -u {svc.pw_name} -- env XDG_RUNTIME_DIR=/run/user/{svc.pw_uid} "
          f"podman secret create {tenant.secret_name('apikey')} -   # or -oauth; value on stdin")


def cmd_list(args: argparse.Namespace) -> None:
    from . import config

    svc = _service_user()
    secrets_out = _as_service_user(svc, "podman", "secret", "ls", "--format", "{{.Name}}")
    secret_names = set(secrets_out.stdout.split())
    units = _as_service_user(svc, "systemctl", "--user", "list-units",
                             "agentbridge-*", "--all", "--output=json", "--no-pager")
    unit_names = [u.get("unit", "") for u in json.loads(units.stdout or "[]")]
    print(f"{'user':<16} {'credential':<10} {'workers':>7}  {'max':>3}")
    for path in sorted(config.TENANTS_DIR.glob("*.json")):
        name = path.stem
        record = json.loads(path.read_text())
        cred = next((k for k in ("apikey", "oauth")
                     if f"anthropic-{name}-{k}" in secret_names), "-")
        count = sum(1 for u in unit_names if u.startswith(f"agentbridge-{name}-"))
        print(f"{name:<16} {cred:<10} {count:>7}  "
              f"{record.get('limits', {}).get('max_workers', '?'):>3}")


def cmd_deprovision(args: argparse.Namespace) -> None:
    from . import config
    from .tenant import TenantError, load_tenant

    try:
        tenant = load_tenant(args.user)
    except TenantError as e:
        sys.exit(str(e))
    svc = _service_user()
    _as_service_user(svc, "systemctl", "--user", "stop", f"{tenant.unit_prefix}*")
    _rabbitmqctl("delete_vhost", tenant.vhost, check=False)
    _rabbitmqctl("delete_user", tenant.bus_user, check=False)
    for path in (config.TENANTS_DIR / f"{tenant.name}.json", tenant.amqp_url_file,
                 tenant.gitconfig):
        path.unlink(missing_ok=True)
    print(f"deprovisioned {tenant.name}: workers stopped, vhost and bus user deleted")
    if args.purge:
        shutil.rmtree(tenant.workspace, ignore_errors=True)
        for kind in ("apikey", "oauth"):
            _as_service_user(svc, "podman", "secret", "rm", tenant.secret_name(kind))
        print(f"  purged workspace {tenant.workspace} and credential secrets")
    else:
        print(f"  kept workspace {tenant.workspace} and credential secrets (use --purge)")
    print(f"  remove the nginx login by hand if wanted: htpasswd -D "
          f"/etc/nginx/.htpasswd-mcp {tenant.name}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentbridge-admin",
                                     description="Manage mcp-agentbridge tenants")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("provision", help="create or update a tenant (idempotent)")
    p.add_argument("user")
    p.add_argument("--max-workers", type=int, default=None)
    p.add_argument("--git-name", help="committer name for implementer commits")
    p.add_argument("--git-email", help="committer email for implementer commits")
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("list", help="list tenants, credentials and worker counts")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("deprovision", help="stop workers, delete vhost and bus user")
    p.add_argument("user")
    p.add_argument("--purge", action="store_true",
                   help="also delete the workspace and credential secrets")
    p.set_defaults(func=cmd_deprovision)

    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("agentbridge-admin must run as root")
    _load_sysconfig()
    args.func(args)


if __name__ == "__main__":
    main()
