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
import subprocess
import sys
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


# cwd="/" on every child: we run as root, often from /root, and the service
# user cannot chdir there — runuser then fails before the command even starts.
def _as_service_user(svc: pwd.struct_passwd, *argv: str) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": svc.pw_dir,
           "XDG_RUNTIME_DIR": f"/run/user/{svc.pw_uid}"}
    return subprocess.run(["runuser", "-u", svc.pw_name, "--", "env",
                           *(f"{k}={v}" for k, v in env.items()), *argv],
                          text=True, capture_output=True, check=False, cwd="/")


def _secret_runner(svc: pwd.struct_passwd):
    """A ``secrets`` runner that reaches the service user's podman store."""
    env = {"PATH": "/usr/bin:/bin", "HOME": svc.pw_dir,
           "XDG_RUNTIME_DIR": f"/run/user/{svc.pw_uid}"}

    def run(argv, stdin=None):
        return subprocess.run(
            ["runuser", "-u", svc.pw_name, "--", "env",
             *(f"{k}={v}" for k, v in env.items()), *argv],
            input=stdin, capture_output=True, check=False, cwd="/",
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
        # setgid (2750): a file created here inherits the directory's group,
        # so the service — which is NOT in nginx's group and therefore cannot
        # chgrp — still leaves a file nginx can read. Without this, every
        # service-side rewrite (provision_user, set_password) silently drops
        # the group and nginx answers 500 to authenticated requests only.
        (directory, 0o2750, web_gid),
        (config.HTPASSWD_FILE, 0o640, web_gid),
        (directory / ".lock", 0o640, svc.pw_gid),
        (config.HTPASSWD_FILE.with_name(config.HTPASSWD_FILE.name + ".bak"),
         0o640, web_gid),
    ):
        if path.exists():
            os.chown(path, svc.pw_uid, gid)
            os.chmod(path, mode)


def cmd_provision(args: argparse.Namespace) -> None:
    from . import config, provisioning
    from .tenant import TenantError

    svc = _service_user()
    try:
        result = provisioning.provision(
            args.user, max_workers=args.max_workers,
            admin=True if args.admin else (False if args.no_admin else None),
            reset_password=args.reset_password,
            owner=(svc.pw_uid, svc.pw_gid),
        )
    except (TenantError, provisioning.rabbit_mgmt.RabbitError) as e:
        sys.exit(str(e))
    _fix_htpasswd_ownership(svc)

    # The git identity stays a CLI convenience for bootstrap; tenants set it
    # themselves with set_git_identity.
    if args.git_name and args.git_email:
        from . import gitconfig
        from .tenant import load_tenant

        tenant = load_tenant(args.user)
        gitconfig.write_identity(tenant, args.git_name, args.git_email)
        os.chown(tenant.gitconfig, svc.pw_uid, svc.pw_gid)
        result["has_git_identity"] = True

    print(f"provisioned {result['user']}: vhost {result['vhost']}, bus user "
          f"{result['bus_user']}, workspace {result['workspace']}, "
          f"max_workers {result['max_workers']}"
          + (", admin" if result["admin"] else ""))
    if not result["has_git_identity"]:
        print("  note: no committer identity (--git-name/--git-email, or the "
              "tenant's own set_git_identity); implementers cannot commit")
    if result["password"]:
        print(f"  login password (shown once, not stored): {result['password']}")
    else:
        print(f"  login unchanged in {config.HTPASSWD_FILE} "
              f"(use --reset-password to issue a new one)")
    print("Remaining manual step (or the tenant's own register_credential):")
    print(f"  runuser -u {svc.pw_name} -- env XDG_RUNTIME_DIR=/run/user/{svc.pw_uid} "
          f"podman secret create anthropic-{result['user']}-apikey -   "
          f"# or -oauth; value on stdin")


def cmd_list(args: argparse.Namespace) -> None:
    from . import provisioning
    from . import secrets as secrets_mod

    svc = _service_user()
    try:
        secret_names = secrets_mod.names(runner=_secret_runner(svc))
    except secrets_mod.SecretError as e:
        sys.exit(str(e))
    units = _as_service_user(svc, "systemctl", "--user", "list-units",
                             "agentbridge-*", "--all", "--output=json", "--no-pager")
    unit_names = [u.get("unit", "") for u in json.loads(units.stdout or "[]")]
    rows = provisioning.list_tenants(secret_names=secret_names, unit_names=unit_names)
    print(f"{'user':<16} {'credential':<10} {'login':<6} {'admin':<6} "
          f"{'workers':>7}  {'max':>3}")
    for row in rows:
        print(f"{row['user']:<16} {','.join(row['credentials']) or '-':<10} "
              f"{('yes' if row['login'] else 'no'):<6} "
              f"{('yes' if row['admin'] else 'no'):<6} "
              f"{row['workers']:>7}  {row['max_workers'] or '?':>3}")


def cmd_deprovision(args: argparse.Namespace) -> None:
    from . import provisioning
    from .tenant import TenantError, load_tenant

    svc = _service_user()
    try:
        tenant = load_tenant(args.user)
    except TenantError as e:
        sys.exit(str(e))
    # Stop the workers before the vhost disappears under them.
    _as_service_user(svc, "systemctl", "--user", "stop", f"{tenant.unit_prefix}*")
    try:
        result = provisioning.deprovision(
            args.user, purge=args.purge, secret_runner=_secret_runner(svc))
    except (TenantError, provisioning.rabbit_mgmt.RabbitError) as e:
        sys.exit(str(e))
    _fix_htpasswd_ownership(svc)

    print(f"deprovisioned {result['user']}: workers stopped, vhost and bus user "
          f"deleted" + (", login removed" if result["login_removed"] else ""))
    if result["purged"]:
        print(f"  purged: {', '.join(result['purged'])}")
    else:
        print(f"  kept workspace {result['workspace_kept']} and credential "
              f"secrets (use --purge)")


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
    p.add_argument("--admin", action="store_true",
                   help="grant access to the admin endpoint's tools")
    p.add_argument("--no-admin", action="store_true",
                   help="revoke admin access")
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
