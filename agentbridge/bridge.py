# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""stdio↔RabbitMQ bridge for AI agent orchestration."""

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
    command: list[str],
) -> None:
    """Run the stdio↔RabbitMQ bridge."""

    # Connect to RabbitMQ
    connection = await aio_pika.connect_robust(amqp_url)
    channel = await connection.channel()

    # Declare exchange (topic type for flexible routing)
    exch = await channel.declare_exchange(
        exchange,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
    )

    # Declare inbox queue — agent receives messages here
    # Binds to both its specific inbox and the broadcast channel
    inbox_queue = await channel.declare_queue(
        f"{agent_id}.inbox",
        durable=True,
    )
    await inbox_queue.bind(exch, routing_key=inbox_topic)
    await inbox_queue.bind(exch, routing_key="*.broadcast")

    # Default outbox routing key follows {agent_id}.{message_type} convention
    effective_outbox = outbox_topic or f"{agent_id}.output"

    # Spawn the subprocess capturing all streams
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,  # capture to detect thinking activity
    )

    logger = logging.getLogger(__name__)
    logger.info("Agent %s started (pid=%d)", agent_id, proc.pid)
    logger.info("Inbox: %s + *.broadcast", inbox_topic)
    logger.info("Outbox: %s", effective_outbox)
    logger.info("Idle timeout: %ss", idle_timeout)

    # Shared activity timestamps
    last_activity: dict[str, float] = {
        "stdout": time.monotonic(),
        "stderr": time.monotonic(),
    }
    waiting_for_orchestrator = asyncio.Event()

    async def stdout_to_rabbit() -> None:
        """Read subprocess stdout, publish to RabbitMQ."""
        async for line in proc.stdout:
            text = line.decode().rstrip()
            if not text:
                continue
            last_activity["stdout"] = time.monotonic()
            msg = json.dumps({
                "agent_id": agent_id,
                "routing_key": effective_outbox,
                "type": "output",
                "body": text,
            })
            await exch.publish(
                aio_pika.Message(
                    body=msg.encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=effective_outbox,
            )
            logger.info("→ %s: %s", effective_outbox, text[:80])

    async def stderr_monitor() -> None:
        """Monitor stderr for activity — Claude CLI shows animation while thinking."""
        async for line in proc.stderr:
            last_activity["stderr"] = time.monotonic()
            # Forward to real stderr so humans can observe
            sys.stderr.write(line.decode())
            sys.stderr.flush()

    async def idle_detector() -> None:
        """Detect when agent is genuinely idle (both stdout and stderr silent)."""
        while proc.returncode is None:
            await asyncio.sleep(5.0)  # check interval
            now = time.monotonic()
            stdout_idle = now - last_activity["stdout"] > idle_timeout
            stderr_idle = now - last_activity["stderr"] > idle_timeout

            if stdout_idle and stderr_idle and not waiting_for_orchestrator.is_set():
                waiting_for_orchestrator.set()
                logger.info("Agent %s idle — notifying orchestrator", agent_id)
                await exch.publish(
                    aio_pika.Message(
                        body=json.dumps({
                            "agent_id": agent_id,
                            "type": "waiting_for_input",
                            "body": f"Agent {agent_id} is idle and waiting for instructions",
                        }).encode(),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ),
                    routing_key=f"{agent_id}.waiting",
                )

    async def rabbit_to_stdin() -> None:
        """Consume RabbitMQ inbox, write to subprocess stdin."""
        async with inbox_queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process():
                    payload = json.loads(message.body.decode())
                    text = payload.get("body", "")
                    proc.stdin.write((text + "\n").encode())
                    await proc.stdin.drain()
                    # Reset activity timestamps and clear waiting flag
                    last_activity["stdout"] = time.monotonic()
                    last_activity["stderr"] = time.monotonic()
                    waiting_for_orchestrator.clear()
                    logger.info("← %s: %s", message.routing_key, text[:80])

    # Run all coroutines concurrently
    await asyncio.gather(
        stdout_to_rabbit(),
        stderr_monitor(),
        idle_detector(),
        rabbit_to_stdin(),
        proc.wait(),
    )

    logger.info("Agent %s exited", agent_id)
    await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="stdio↔RabbitMQ bridge for AI agent orchestration"
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
        help="Routing key for incoming messages (e.g. researcher-a1b2.inbox)",
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
        help="Seconds of silence before notifying orchestrator (default: 60)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to wrap (e.g. claude)",
    )
    args = parser.parse_args()

    if not args.command:
        parser.error("No command specified — provide a command to wrap after --")

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
    logger.info("Starting agent: %s", agent_id)

    asyncio.run(bridge(
        agent_id=agent_id,
        amqp_url=args.amqp_url,
        exchange=args.exchange,
        inbox_topic=args.inbox,
        outbox_topic=args.outbox,
        idle_timeout=args.idle_timeout,
        command=args.command,
    ))


if __name__ == "__main__":
    main()
