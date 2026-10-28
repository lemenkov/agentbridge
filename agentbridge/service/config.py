# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Service configuration, read once from the environment (/etc/sysconfig)."""

import os
import sys
from pathlib import Path

# Repo checkout holding AGENT.md and SKILLS/ — the parent of this package.
AGENTBRIDGE_HOME = Path(
    os.environ.get("AGENTBRIDGE_HOME", Path(__file__).resolve().parents[2])
)

# Per-tenant records: <u>.json (bus credentials, limits), <u>.amqp (bus URL
# for the bridge's --amqp-url-file), optional <u>.gitconfig.
TENANTS_DIR = Path(
    os.environ.get("TENANTS_DIR", "/var/lib/mcp-agentbridge/tenants")
)
# Per-tenant workspaces: <WORKSPACES_DIR>/<u>/
WORKSPACES_DIR = Path(os.environ.get("WORKSPACES_DIR", "/srv/agentbridge"))

USER_HEADER = os.environ.get("USER_HEADER", "x-forwarded-user").lower()
AMQP_HOST = os.environ.get("AMQP_HOST", "localhost:5672")
EXCHANGE = os.environ.get("AGENTBRIDGE_EXCHANGE", "agents")

# Where the admin credentials live, named in errors so the operator knows
# which file to edit.
SYSCONFIG_PATH = os.environ.get("SYSCONFIG_PATH", "/etc/sysconfig/mcp-agentbridge")

# RabbitMQ management API: loopback only (plan 0.1a), reached with an
# administrator account instead of rabbitmqctl's Erlang cookie (plan 0.4).
RABBITMQ_API = os.environ.get("RABBITMQ_API", "http://127.0.0.1:15672/api")
RABBITMQ_ADMIN_USER = os.environ.get("RABBITMQ_ADMIN_USER", "")
RABBITMQ_ADMIN_PASSWORD = os.environ.get("RABBITMQ_ADMIN_PASSWORD", "")

# This service's own nginx password file — never /etc/nginx/.htpasswd-mcp,
# which is server-level and grants every fleet MCP.
HTPASSWD_FILE = Path(
    os.environ.get("HTPASSWD_FILE", "/var/lib/mcp-agentbridge/htpasswd/users")
)

# The bridge runs from the same venv as the service.
BRIDGE_BIN = os.environ.get(
    "AGENTBRIDGE_BIN", str(Path(sys.executable).parent / "agentbridge")
)
# Host path of the native `claude` binary, bind-mounted read-only into leaves.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "")

# Workers run claude through the stream-json driver, which is what makes
# per-task metrics and tool progress available. `claude-text` is the fallback.
AGENT_DRIVER = os.environ.get("AGENT_DRIVER", "claude-stream-json")

IDLE_TIMEOUT = int(os.environ.get("WORKER_IDLE_TIMEOUT", "45"))
DEFAULT_MAX_WORKERS = int(os.environ.get("DEFAULT_MAX_WORKERS", "4"))

# Generic per-worker resource caps, enforced by systemd on the worker unit (the
# containers run inside it via podman --cgroups=split).
WORKER_MEMORY_MAX = os.environ.get("WORKER_MEMORY_MAX", "3G")
WORKER_CPU_QUOTA = os.environ.get("WORKER_CPU_QUOTA", "200%")
WORKER_TASKS_MAX = os.environ.get("WORKER_TASKS_MAX", "512")

MAX_COLLECT_SECONDS = int(os.environ.get("MAX_COLLECT_SECONDS", "120"))
INLINE_MAX_BYTES = int(os.environ.get("INLINE_MAX_BYTES", "4096"))
WORKSPACE_READ_MAX = int(os.environ.get("WORKSPACE_READ_MAX", "262144"))
CLONE_TIMEOUT = int(os.environ.get("CLONE_TIMEOUT", "300"))
