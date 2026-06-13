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
agentbridge \
    --agent researcher-01 \
    --inbox researcher-01.inbox \
    --amqp-url amqp://agentbridge:YOURPASSWORD@rabbitmq.host/ \
    --preamble-file AGENT.md --preamble-file SKILLS/researcher.md \
    claude --print --dangerously-skip-permissions
```

The wrapped command carries **no task** — the worker feeds each task to the
agent's stdin as it arrives. To give it work, publish a task to its inbox:

```
routing key: researcher-01.inbox    body: {"body": "<task text>"}
```

- `--amqp-url` is **required** — the bridge refuses to start without it.
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

## Requirements

- Python 3.10+
- RabbitMQ
- `aio-pika`

## License

Apache-2.0
