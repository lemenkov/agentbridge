# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""agentbridge-admin: provision, list and deprovision tenants. Run as root.

    agentbridge-admin provision petro --git-name "..." --git-email "..."
    agentbridge-admin list
    agentbridge-admin deprovision petro [--purge]

A thin CLI over the same library functions the service uses, kept for
bootstrap and recovery. The broker side goes through the management API
(``rabbit_mgmt``), so there is no ``rabbitmqctl`` and no Erlang cookie here
any more; the login goes into the service's own password file
(``htpasswd``), never the fleet-wide one.

Two secrets, handled differently. The bus password is service plumbing:
generated here, sent to the broker only as a salted hash, stored 0600 for the
service user, never shown. The login password is the user's: generated here
and printed **once**, stored only as bcrypt. A user's Anthropic credential is
registered out of band as a podman secret (printed as the last step).
"""

import argparse
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

SYSCONFIG = Path(os.environ.get("SYSCONFIG_PATH", "/etc/sysconfig/mcp-agentbridge"))


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


def _as_service_user(svc: pwd.struct_passwd, *argv: str) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": svc.pw_dir,
           "XDG_RUNTIME_DIR": f"/run/user/{svc.pw_uid}"}
    return subprocess.run(["runuser", "-u", svc.pw_name, "--", "env",
                           *(f"{k}={v}" for k, v in env.items()), *argv],
                          text=True, capture_output=True, check=False)


def _secret_runner(svc: pwd.struct_passwd):
    """A ``secrets`` runner that reaches the service user's podman store."""
    env = {"PATH": "/usr/bin:/bin", "HOME": svc.pw_dir,
           "XDG_RUNTIME_DIR": f"/run/user/{svc.pw_uid}"}

    def run(argv, stdin=None):
        return subprocess.run(
            ["runuser", "-u", svc.pw_name, "--", "env",
             *(f"{k}={v}" for k, v in env.items()), *argv],
            input=stdin, capture_output=True, check=False,
        )

    return run


def _write_private(path: Path, text: str, svc: pwd.struct_passwd) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    os.chown(path, svc.pw_uid, svc.pw_gid)


def _fix_htpasswd_ownership(svc: pwd.struct_passwd) -> None:
    """Hand the password file back to the service user and nginx's group.

    We write it as root, so without this the file, its lock and its backup end
    up root-owned: nginx then cannot read the file (a 500 on every
    *authenticated* request, which looks nothing like an auth problem), and the
    service cannot take the lock to change a password later.
    """
    import grp

    from . import config

    try:
        web_gid = grp.getgrnam("nginx").gr_gid
    except KeyError:
        web_gid = svc.pw_gid
    directory = config.HTPASSWD_FILE.parent
    for path, mode, gid in (
        (directory, 0o750, web_gid),
        (config.HTPASSWD_FILE, 0o640, web_gid),
        (directory / ".lock", 0o640, svc.pw_gid),
        (config.HTPASSWD_FILE.with_name(config.HTPASSWD_FILE.name + ".bak"),
         0o640, web_gid),
    ):
        if path.exists():
            os.chown(path, svc.pw_uid, gid)
            os.chmod(path, mode)


def cmd_provision(args: argparse.Namespace) -> None:
    from . import config, htpasswd, rabbit_mgmt
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

    # Broker: vhost + a user permitted on that vhost only, over the management
    # API. Every call is a PUT, so re-running simply resyncs the broker to the
    # record — no delete-then-recreate, as rabbitmqctl needed.
    try:
        rabbit_mgmt.ensure_tenant(tenant.vhost, tenant.bus_user, tenant.bus_password)
    except rabbit_mgmt.RabbitError as e:
        sys.exit(str(e))

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

    # Login: our own password file, not the fleet-wide one. A generated
    # password is printed once and never stored in the clear.
    new_password = None
    if args.reset_password or not htpasswd.has_entry(name):
        new_password = htpasswd.generate_password()
        htpasswd.set_entry(name, new_password)
    _fix_htpasswd_ownership(svc)

    print(f"provisioned {name}: vhost {tenant.vhost}, bus user {tenant.bus_user}, "
          f"workspace {tenant.workspace}, max_workers {tenant.max_workers}")
    if not tenant.gitconfig.exists():
        print("  note: no committer identity (--git-name/--git-email); "
              "implementers will not be able to commit")
    if new_password:
        print(f"  login password (shown once, not stored): {new_password}")
    else:
        print(f"  login unchanged in {config.HTPASSWD_FILE} "
              f"(use --reset-password to issue a new one)")
    print("Remaining manual step:")
    print(f"  runuser -u {svc.pw_name} -- env XDG_RUNTIME_DIR=/run/user/{svc.pw_uid} "
          f"podman secret create {tenant.secret_name('apikey')} -   # or -oauth; value on stdin")


def cmd_list(args: argparse.Namespace) -> None:
    from . import config, htpasswd
    from . import secrets as secrets_mod

    svc = _service_user()
    try:
        secret_names = secrets_mod.names(runner=_secret_runner(svc))
    except secrets_mod.SecretError as e:
        sys.exit(str(e))
    units = _as_service_user(svc, "systemctl", "--user", "list-units",
                             "agentbridge-*", "--all", "--output=json", "--no-pager")
    unit_names = [u.get("unit", "") for u in json.loads(units.stdout or "[]")]
    logins = set(htpasswd.entries())
    print(f"{'user':<16} {'credential':<10} {'login':<6} {'workers':>7}  {'max':>3}")
    for path in sorted(config.TENANTS_DIR.glob("*.json")):
        name = path.stem
        record = json.loads(path.read_text())
        cred = next((k for k in ("apikey", "oauth")
                     if f"anthropic-{name}-{k}" in secret_names), "-")
        count = sum(1 for u in unit_names if u.startswith(f"agentbridge-{name}-"))
        print(f"{name:<16} {cred:<10} {('yes' if name in logins else 'no'):<6} {count:>7}  "
              f"{record.get('limits', {}).get('max_workers', '?'):>3}")


def cmd_deprovision(args: argparse.Namespace) -> None:
    from . import config, htpasswd, rabbit_mgmt
    from . import secrets as secrets_mod
    from .tenant import TenantError, load_tenant

    try:
        tenant = load_tenant(args.user)
    except TenantError as e:
        sys.exit(str(e))
    svc = _service_user()
    _as_service_user(svc, "systemctl", "--user", "stop", f"{tenant.unit_prefix}*")
    try:
        rabbit_mgmt.delete_vhost(tenant.vhost)
        rabbit_mgmt.delete_user(tenant.bus_user)
    except rabbit_mgmt.RabbitError as e:
        sys.exit(str(e))
    had_login = htpasswd.remove_entry(tenant.name)
    _fix_htpasswd_ownership(svc)
    for path in (config.TENANTS_DIR / f"{tenant.name}.json", tenant.amqp_url_file,
                 tenant.gitconfig):
        path.unlink(missing_ok=True)
    print(f"deprovisioned {tenant.name}: workers stopped, vhost and bus user deleted"
          + (", login removed" if had_login else ""))
    if args.purge:
        shutil.rmtree(tenant.workspace, ignore_errors=True)
        runner = _secret_runner(svc)
        for kind in ("apikey", "oauth"):
            secrets_mod.remove(tenant.secret_name(kind), runner=runner)
        print(f"  purged workspace {tenant.workspace} and credential secrets")
    else:
        print(f"  kept workspace {tenant.workspace} and credential secrets (use --purge)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentbridge-admin",
                                     description="Manage mcp-agentbridge tenants")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("provision", help="create or update a tenant (idempotent)")
    p.add_argument("user")
    p.add_argument("--max-workers", type=int, default=None)
    p.add_argument("--git-name", help="committer name for implementer commits")
    p.add_argument("--git-email", help="committer email for implementer commits")
    p.add_argument("--reset-password", action="store_true",
                   help="issue a new login password even if one exists")
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
