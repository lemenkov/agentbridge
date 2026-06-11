<!--
SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# agentbridge

Multi-agent orchestration bridge connecting AI agents via a message bus.

## Overview

`agentbridge` wraps any stdio-based AI agent (e.g. the `claude` CLI) and
connects it to a RabbitMQ topic exchange: messages published to the agent's
inbox are written to its stdin, and everything the agent writes to stdout is
published to its outbox. An orchestrator can then coordinate many agents
asynchronously over the bus, with the agents fully decoupled from one another.

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
    claude --dangerously-skip-permissions --print "Your task here"
```

- `--amqp-url` is **required** — the bridge refuses to start without it.
- The wrapped command follows the bridge's own flags directly. Do **not** place
  a `--` separator before it; the trailing argv is passed through verbatim.
- `--outbox` defaults to `{agent_id}.output`. An idle agent is announced on
  `{agent_id}.waiting` after `--idle-timeout` seconds (default 60), so the
  orchestrator can send it the next instruction.

## Requirements

- Python 3.10+
- RabbitMQ
- `aio-pika`

## License

Apache-2.0
