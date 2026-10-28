#!/bin/bash
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# One-time (idempotent) host setup for mcp-agentbridge. Run as root:
#
#   sudo deploy/setup-host.sh [path-to-claude-binary]
#
# Re-run it to update the claude binary the leaves use. The claude source
# defaults to the native install (~/.local/bin/claude) of the sudo user, or of
# the repo's owner when run as root directly.
# It does NOT touch nginx, RabbitMQ's listener config, or any tenant — those
# steps are printed at the end.

set -euo pipefail

SVC=agentbridge
SVC_HOME=/var/lib/mcp-agentbridge
# Physical path: the service user must be able to reach it, and a symlinked
# path through a 0700 home directory would not be.
REPO=$(cd "$(dirname "$0")/.." && pwd -P)
LIBEXEC=/usr/local/libexec/agentbridge

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

# --- service user: real user (rootless podman + user systemd need one) -------
if ! id "$SVC" &>/dev/null; then
    useradd --system --create-home --home-dir "$SVC_HOME" \
            --shell /sbin/nologin --comment "mcp-agentbridge service" "$SVC"
    echo "created user $SVC"
fi
SVC_UID=$(id -u "$SVC")

# subordinate ids: a fresh 65536 range after the highest one in use
for f in /etc/subuid /etc/subgid; do
    if ! grep -q "^$SVC:" "$f"; then
        start=$(awk -F: 'BEGIN{m=100000} {e=$2+$3; if(e>m)m=e} END{print m}' "$f")
        echo "$SVC:$start:65536" >> "$f"
        echo "added $SVC:$start:65536 to $f"
    fi
done

loginctl enable-linger "$SVC"
# wait for the user manager so the podman/systemctl calls below work
for _ in $(seq 1 20); do
    [[ -S /run/user/$SVC_UID/bus ]] && break
    sleep 0.5
done

as_svc() {
    runuser -u "$SVC" -- env HOME="$SVC_HOME" XDG_RUNTIME_DIR="/run/user/$SVC_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$SVC_UID/bus" "$@"
}

# --- directories --------------------------------------------------------------
install -d -m 0700 -o "$SVC" -g "$SVC" "$SVC_HOME/tenants"
install -d -m 0750 -o "$SVC" -g "$SVC" /srv/agentbridge

# This service's own nginx password file. Three permissions matter, and getting
# any of them wrong produces the same confusing symptom — nginx answers 500 to
# requests that CARRY credentials while unauthenticated ones still get a clean
# 401, which looks nothing like a permissions problem:
#
#   0711 on $SVC_HOME   so nginx can traverse to the directory below
#   2750 on htpasswd/   setgid, so a file created here inherits nginx's group.
#                       The service is not in that group and so cannot chgrp;
#                       without setgid every service-side rewrite (set_password,
#                       provision_user) silently drops the group.
#   0640 agentbridge:nginx on the file itself
WEB_GROUP=$(getent group nginx >/dev/null && echo nginx || echo "$SVC")
chmod 0711 "$SVC_HOME"
install -d -m 2750 -o "$SVC" -g "$WEB_GROUP" "$SVC_HOME/htpasswd"

# --- claude binary, mounted read-only into leaves -----------------------------
SRC=${1:-}
if [[ -z $SRC ]]; then
    owner=${SUDO_USER:-$(stat -c %U "$REPO")}
    SRC=$(readlink -f "$(getent passwd "$owner" | cut -d: -f6)/.local/bin/claude" || true)
fi
if [[ -n $SRC && -x $SRC ]]; then
    install -d -m 0755 "$LIBEXEC"
    install -m 0755 "$SRC" "$LIBEXEC/claude"
    echo "installed claude $("$LIBEXEC/claude" --version 2>/dev/null | head -1) -> $LIBEXEC/claude"
else
    echo "WARNING: no claude binary found; pass its path as the first argument" >&2
fi

# --- leaf images, in the service user's rootless store ------------------------
as_svc podman build -q -t agentbridge-base  -f "$REPO/images/base.Containerfile"  "$REPO/images"
as_svc podman build -q -t agentbridge-pydev -f "$REPO/images/pydev.Containerfile" "$REPO/images"

# --- unit + config ------------------------------------------------------------
if [[ ! -e /etc/sysconfig/mcp-agentbridge ]]; then
    install -m 0644 "$REPO/deploy/mcp-agentbridge.sysconfig" /etc/sysconfig/mcp-agentbridge
    echo "installed /etc/sysconfig/mcp-agentbridge"
else
    echo "kept existing /etc/sysconfig/mcp-agentbridge (compare with deploy/mcp-agentbridge.sysconfig)"
fi
install -m 0644 "$REPO/deploy/mcp-agentbridge.service" /etc/systemd/system/mcp-agentbridge.service
systemctl daemon-reload
systemctl enable mcp-agentbridge.service
systemctl restart mcp-agentbridge.service   # re-runs pick up unit/code changes
systemctl --no-pager --lines=0 status mcp-agentbridge.service || true

cat <<EOF

Done. Remaining manual steps:

1. nginx: add deploy/nginx-location.conf to the 8443 server block in
   /etc/nginx/conf.d/mcp.conf, then: nginx -t && systemctl reload nginx

2. RabbitMQ: it listens on *:5672, which leaf containers can reach via the
   host's address. Bind it to loopback in /etc/rabbitmq/rabbitmq.conf:
       listeners.tcp.local = 127.0.0.1:5672
   then: systemctl restart rabbitmq-server

3. Per user:
       $REPO/venv/bin/agentbridge-admin provision <user> \\
           --git-name "Full Name" --git-email user@example.com
       # provision writes this service's OWN password file
       # ($SVC_HOME/htpasswd/users) and prints the one-time password. Do NOT
       # add the user to /etc/nginx/.htpasswd-mcp — that file is server-level
       # and would grant every MCP on this host.
       runuser -u $SVC -- env XDG_RUNTIME_DIR=/run/user/$SVC_UID \\
           podman secret create anthropic-<user>-apikey -    # or -oauth (claude setup-token)

4. Verify through nginx, WITH credentials. Every other check can be done on
   loopback, but only this one exercises Basic auth and the password file's
   permissions — the failure mode above is invisible to a loopback test:
       curl -u <user> https://<host>/mcp/agentbridge \
           -H 'Content-Type: application/json' \
           -H 'Accept: application/json, text/event-stream' \
           -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
   Expect a JSON-RPC result naming the server. A 500 here means nginx cannot
   read the password file; re-run `agentbridge-admin provision <user>`, which
   repairs its ownership and mode.
EOF
