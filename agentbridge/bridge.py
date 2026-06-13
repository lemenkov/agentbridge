# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""stdio↔RabbitMQ bridge / pull-worker for AI agent orchestration.

Runs on the host as a long-lived *pull-worker*: it consumes task messages from
a RabbitMQ queue and, for each one, spawns the wrapped agent command fresh,
feeds the task to its stdin, and republishes the agent's stdout to the bus. The
agent process (e.g. ``claude --print``) is stateless and disposable — the
worker (this bridge) is what persists and pulls the next task.

A task is acknowledged only after the agent process exits, so a worker that
crashes mid-task leaves the task unacked and the broker redelivers it.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid

import aio_pika


def generate_agent_id(prefix: str = "agent") -> str:
    """Generate a unique predictable agent ID."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def bridge(
    agent_id: str,
    amqp_url: str,
    exchange: str,
    inbox_topic: str,
    outbox_topic: str | None,
    idle_timeout: float,
    preamble: str,
    command: list[str],
) -> None:
    """Run the pull-worker loop: consume tasks, run the agent, republish output."""

    connection = await aio_pika.connect_robust(amqp_url)
    channel = await connection.channel()
    # Pull one task at a time; this is also what makes a pool of workers sharing
    # one queue load-balance fairly (competing consumers).
    await channel.set_qos(prefetch_count=1)

    exch = await channel.declare_exchange(
        exchange,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
    )

    # The worker consumes from its inbox queue, bound to its own routing key and
    # the broadcast channel.
    inbox_queue = await channel.declare_queue(f"{agent_id}.inbox", durable=True)
    await inbox_queue.bind(exch, routing_key=inbox_topic)
    await inbox_queue.bind(exch, routing_key="*.broadcast")

    outbox = outbox_topic or f"{agent_id}.output"
    waiting_key = f"{agent_id}.waiting"

    logger = logging.getLogger(__name__)
    logger.info("Worker %s ready", agent_id)
    logger.info("Inbox: %s + *.broadcast", inbox_topic)
    logger.info("Outbox: %s | Idle timeout: %ss", outbox, idle_timeout)
    logger.info("Agent command: %s", " ".join(command))

    async def publish(routing_key: str, obj: dict) -> None:
        await exch.publish(
            aio_pika.Message(
                body=json.dumps(obj).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
        )

    async def run_task(task_text: str) -> int:
        """Spawn the agent fresh, feed it the task on stdin, republish stdout."""
        payload = f"{preamble}\n\nYour task: {task_text}" if preamble else task_text

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(payload.encode())
        await proc.stdin.drain()
        proc.stdin.close()  # EOF so the agent starts processing the prompt

        async def pump_stdout() -> None:
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                await publish(outbox, {
                    "agent_id": agent_id,
                    "routing_key": outbox,
                    "type": "output",
                    "body": text,
                })
                logger.info("→ %s: %s", outbox, text[:80])

        async def pump_stderr() -> None:
            async for line in proc.stderr:
                # Forward to real stderr so humans/journald can observe.
                sys.stderr.write(line.decode(errors="replace"))
                sys.stderr.flush()

        await asyncio.gather(pump_stdout(), pump_stderr())
        return await proc.wait()

    last_activity = time.monotonic()
    idle_announced = False
    stop = False

    while not stop:
        # basic_get polling (not an async iterator): a timed-out iterator read
        # corrupts aio_pika's queue iterator, so we poll instead.
        message = await inbox_queue.get(fail=False)
        if message is None:
            if not idle_announced and (time.monotonic() - last_activity) > idle_timeout:
                await publish(waiting_key, {
                    "agent_id": agent_id,
                    "type": "waiting_for_input",
                    "body": f"Agent {agent_id} is idle and waiting for instructions",
                })
                logger.info("Agent %s idle — announced on %s", agent_id, waiting_key)
                idle_announced = True
            await asyncio.sleep(1.0)
            continue

        # Ack only after the task completes: msg.process() acks on clean exit and
        # rejects on exception, and a worker crash leaves it unacked → redelivered.
        async with message.process():
            try:
                payload = json.loads(message.body.decode())
            except json.JSONDecodeError:
                payload = {"body": message.body.decode(errors="replace")}

            if payload.get("type") == "shutdown":
                logger.info("Shutdown requested — retiring worker %s", agent_id)
                stop = True
            else:
                task = payload.get("body", "")
                logger.info("← task: %s", task[:80])
                rc = await run_task(task)
                logger.info("Task complete (rc=%d)", rc)

        last_activity = time.monotonic()
        idle_announced = False

    await connection.close()
    logger.info("Worker %s exited", agent_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="stdio↔RabbitMQ bridge / pull-worker for AI agent orchestration"
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent ID (default: auto-generated)",
    )
    parser.add_argument(
        "--agent-prefix",
        default="agent",
        help="Prefix for auto-generated agent ID (default: agent)",
    )
    parser.add_argument(
        "--amqp-url",
        required=True,
        help="RabbitMQ AMQP URL (required). The bridge refuses to start without "
             "it — there is no default.",
    )
    parser.add_argument(
        "--exchange",
        default="agents",
        help="RabbitMQ exchange name (default: agents)",
    )
    parser.add_argument(
        "--inbox",
        required=True,
        help="Routing key for incoming task messages (e.g. researcher-a1b2.inbox)",
    )
    parser.add_argument(
        "--outbox",
        default=None,
        help="Routing key for outgoing messages (default: {agent_id}.output)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=60.0,
        help="Seconds with no task before announcing idle (default: 60)",
    )
    parser.add_argument(
        "--preamble-file",
        action="append",
        default=[],
        metavar="PATH",
        help="File prepended to every task as the agent's role preamble "
             "(repeatable; concatenated in order). Read host-side, so it never "
             "needs to be mounted into the agent's sandbox.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Agent command to spawn per task (e.g. claude --print). The task is "
             "fed to its stdin; its stdout is republished to the bus.",
    )
    args = parser.parse_args()

    if not args.command:
        parser.error("No command specified — provide a command to wrap after the options")

    agent_id = args.agent or generate_agent_id(args.agent_prefix)

    # Set up logging — journald if available, fallback to stderr
    try:
        from systemd.journal import JournalHandler
        handler: logging.Handler = JournalHandler(
            SYSLOG_IDENTIFIER=f"agentbridge-{agent_id}",
        )
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(f"[agentbridge-{agent_id}] %(message)s"))

    logging.basicConfig(handlers=[handler], level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.info("Starting worker: %s", agent_id)

    preamble = "\n\n".join(
        open(path, encoding="utf-8").read() for path in args.preamble_file
    )

    asyncio.run(bridge(
        agent_id=agent_id,
        amqp_url=args.amqp_url,
        exchange=args.exchange,
        inbox_topic=args.inbox,
        outbox_topic=args.outbox,
        idle_timeout=args.idle_timeout,
        preamble=preamble,
        command=args.command,
    ))


if __name__ == "__main__":
    main()
