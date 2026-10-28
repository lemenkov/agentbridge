<!--
SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# agentbridge

Multi-agent orchestration bridge connecting AI agents via a message bus.

## Overview

`agentbridge` wraps a stdio-based AI agent (e.g. the `claude` CLI) and turns it
into a RabbitMQ **pull-worker**: it consumes task messages from the agent's
inbox queue and, for each one, runs the agent fresh with that task on stdin and
republishes the agent's stdout to the bus. The agent runs once per task and
exits — it is stateless and disposable; the bridge persists and pulls the next.
An orchestrator coordinates many such workers asynchronously over the bus.

## Architecture

```
Orchestrator (human-supervised)
    │   publishes tasks → {agent}.inbox     consumes ← *.output / *.waiting
    │
    ├── agentbridge ── claude  "researcher"
    │       stdin  ← {agent}.inbox  (+ *.broadcast)
    │       stdout → {agent}.output
    │       idle   → {agent}.waiting
    │
    └── agentbridge ── claude  "implementer"
            ...
```

All traffic flows through one durable topic exchange (`agents` by default).

The orchestrator is **not** wrapped by `agentbridge` — it runs as a normal,
human-supervised `claude` session (its stdin/stdout are the operator's
terminal) and reads [ORCHESTRATOR.md](ORCHESTRATOR.md) as its bootstrap. Only
the agents it spawns are wrapped by `agentbridge`. That document also covers
the full orchestration model, including running each agent's `claude` inside an
isolated podman container.

## Prerequisites

- A running **RabbitMQ** broker, reachable from wherever the bridge runs.
- A bus user for the bridge to authenticate as. Create one and grant it full
  permissions on the vhost it will use:

  ```bash
  rabbitmqctl add_user agentbridge YOURPASSWORD
  rabbitmqctl set_permissions agentbridge ".*" ".*" ".*"
  ```

- **Python 3.10+**, and the wrapped agent CLI (e.g. `claude`) available on the
  host.

## Install

```bash
python3 -m venv venv
venv/bin/pip install -e .                 # runtime dependency: aio-pika
venv/bin/pip install -e ".[systemd]"      # optional: journald logging
                                          # (needs gcc, pkg-config, systemd-devel)
```

Without the `systemd` extra the bridge logs to stderr instead of journald.

This installs two console scripts: **`agentbridge`** (the per-agent worker
bridge) and **`agentbus`** (the orchestrator's bus helper — `setup` the
collector queue, `send` tasks/shutdowns, `collect` agent output).

## Running the orchestrator

A human only ever starts the **orchestrator** — a plain `claude` session run
inside this repository, which picks up `ORCHESTRATOR.md` (symlinked as
`CLAUDE.md`) as its bootstrap. Hand it the bus URL via the environment:

```bash
AMQP_URL=amqp://agentbridge:YOURPASSWORD@rabbitmq.host/ claude
```

(or just run `claude` and give it the URL when it asks). From there the
orchestrator spawns and coordinates agents on its own — see below.

## Invocation

You do not run `agentbridge` by hand. The **orchestrator** launches it — once
per agent it spawns — to bridge that agent's stdio to the bus. In practice the
orchestrator wraps the call in `podman run` so the agent's `claude` runs in an
isolated container; see [ORCHESTRATOR.md](ORCHESTRATOR.md) for that recipe.

For reference, the bridge's own CLI contract is:

```bash
AMQP_URL=amqp://agentbridge:YOURPASSWORD@rabbitmq.host/ agentbridge \
    --agent researcher-01 \
    --inbox researcher-01.inbox \
    --preamble-file AGENT.md --preamble-file SKILLS/researcher.md \
    claude --print --dangerously-skip-permissions
```

The wrapped command carries **no task** — the worker feeds each task to the
agent's stdin as it arrives. To give it work, publish a task to its inbox:

```
routing key: researcher-01.inbox    body: {"body": "<task text>"}
```

- The bus URL is **required** — from `--amqp-url`, else `--amqp-url-file PATH`
  (first line), else `$AMQP_URL`; the bridge refuses to start with none of
  them. Prefer the file or the environment when the URL carries a password:
  `--amqp-url` puts it in the process list.
- By default the bridge logs only task ids, byte counts and exit codes — never
  task text, agent output or agent stderr. `--log-content` turns those back on
  for debugging.
- `--preamble-file` (repeatable) is prepended to every task as the agent's role
  context; it is read host-side, so it needn't be mounted into a sandbox.
- The wrapped command follows the bridge's own flags directly, takes **no
  prompt argument** (`claude --print` reads the task from stdin), and must not
  be preceded by a `--` separator.
- `--work-queue NAME` (optional) makes the worker also consume a shared queue.
  Start several workers with the same `--work-queue` and the broker
  load-balances tasks published to that routing key across them (a worker pool /
  competing consumers); the per-agent inbox still receives directed messages.
- `--outbox` defaults to `{agent_id}.output`. An idle worker is announced on
  `{agent_id}.waiting` after `--idle-timeout` seconds (default 60). Retire it
  with a `{"type": "shutdown"}` message to its inbox.
- `--model MODEL` / `--effort LEVEL` set defaults appended to the wrapped
  command for every task (claude's own flags). With `--allow-task-overrides` a
  task message may carry `"model"` / `"effort"` to override them for that task;
  values are allowlisted (model alias or `claude-…` id; effort `low`…`max`), and
  a task with an invalid value is refused with `rc=2` instead of being run.
- Each stdout line is published as its own `{"type": "output"}` message; when
  the agent exits, the bridge publishes `{"type": "done", "rc": N}` on the same
  outbox. A task may carry a `"task_id"`, which is echoed on its output and
  done messages (`agentbus send` adds one automatically).

## MCP service (`mcp-agentbridge`)

The same fleet can be driven over MCP by any number of users, each from the
Claude they already use (claude.ai or Claude Code). `mcp-agentbridge` runs as
one service behind nginx and scopes every tool call to the caller named by
nginx's `X-Forwarded-User` (the Basic-auth user):

| Per user `<u>` | |
|---|---|
| Bus | RabbitMQ vhost `ab-<u>`, bus user `ab-<u>` permitted only there |
| Credential | podman secret `anthropic-<u>-apikey` (or `-oauth`), injected into that user's containers only |
| Workspace | `/srv/agentbridge/<u>/`, mounted at `/workspace` (ro or rw by role) |
| Workers | transient systemd user units `agentbridge-<u>-<role>-<n>`, capped by `MemoryMax`/`CPUQuota`/`TasksMax` |

Workers can be given a default model and effort at spawn
(`spawn_worker(role, model="opus", effort="high")`), and any single task can
override them (`send_task(..., model="haiku", effort="low")`).

Each finished task appends one metadata line — ids, model, exit code, duration,
turns, tokens, cost — to a per-user ledger kept **outside** the workspace, so an
agent cannot edit the record of what it did. `task_history` and `usage_summary`
read it back, and it is written whether or not anyone calls `collect`. Workers
run claude through a `stream-json` driver, which is also what makes tool-call
progress available on `{agent}.progress`.

Tools: `whoami`, `list_roles`, `spawn_worker`, `list_workers`, `send_task`,
`broadcast`, `collect`, `retire_worker`, `retire_all`, `workspace_ls`,
`workspace_read`, `workspace_write`, `clone_repo`, `task_history`,
`usage_summary`, plus `register_credential`, `revoke_credential`,
`set_password` and `set_git_identity` for your own account. Roles, images and mounts are
server-side allowlists; no tool argument is a host path, image or flag. Leaves
use rootless default networking (no `--network=host`), so they cannot reach
host loopback services.

```bash
pip install -e ".[systemd,mcp]"                 # into this repo's venv
sudo deploy/setup-host.sh                       # service user, images, unit
sudo venv/bin/agentbridge-admin provision <u> --git-name "..." --git-email "..."
```

Provisioning lives on a **separate endpoint** (`/mcp/agentbridge-admin`, its own
port) so that an orchestrator reading untrusted agent output holds no handle to
the tools that create and delete tenants; reaching it is not enough, the
caller's tenant record must also say `"admin": true`.

`setup-host.sh` prints the remaining manual steps: the nginx locations
(`deploy/nginx-location.conf`), binding RabbitMQ to loopback, and registering
each user's Anthropic credential out of band. That credential is the only
secret a user hands over; it sits in a 0600 store on a host the operator is
root on, so users should register a dedicated, spend-capped key and watch
their own usage.

**Read [SECURITY.md](SECURITY.md) before provisioning a second user.** It states
the threat model, what the service enforces, and the known limitations — in
particular that the identity header is trusted unconditionally, so any local
process on the host can act as any tenant.

## Requirements

- Python 3.10+
- RabbitMQ
- `aio-pika`

## License

Apache-2.0
