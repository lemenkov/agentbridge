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
import os
import sys
import time
import uuid
from collections.abc import Mapping

import aio_pika


def resolve_amqp_url(
    flag: str | None, path: str | None, environ: Mapping[str, str]
) -> str:
    """Resolve the bus URL: --amqp-url, else --amqp-url-file, else $AMQP_URL.

    There is deliberately no default: with none of the three set this raises.
    """
    if flag:
        return flag
    if path:
        with open(path, encoding="utf-8") as fh:
            url = fh.readline().strip()
        if not url:
            raise ValueError(f"--amqp-url-file {path} is empty")
        return url
    url = environ.get("AMQP_URL", "").strip()
    if url:
        return url
    raise ValueError(
        "no AMQP URL — pass --amqp-url, --amqp-url-file, or set $AMQP_URL "
        "(there is no default)"
    )


def generate_agent_id(prefix: str = "agent") -> str:
    """Generate a unique predictable agent ID."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def bridge(
    agent_id: str,
    amqp_url: str,
    exchange: str,
    inbox_topic: str,
    work_topic: str | None,
    outbox_topic: str | None,
    idle_timeout: float,
    preamble: str,
    command: list[str],
    log_content: bool = False,
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

    # The worker consumes from its own inbox queue (directed messages), bound to
    # its routing key and the broadcast channel.
    inbox_queue = await channel.declare_queue(f"{agent_id}.inbox", durable=True)
    await inbox_queue.bind(exch, routing_key=inbox_topic)
    await inbox_queue.bind(exch, routing_key="*.broadcast")

    # Queues are polled in order, so the directed inbox takes priority over the
    # shared pool.
    queues = [inbox_queue]

    # Optionally also consume a shared work queue: every worker given the same
    # --work-queue name declares the same durable queue, so the broker hands each
    # task published to that routing key to exactly one of them (competing
    # consumers / a worker pool). It is NOT bound to *.broadcast — a broadcast to
    # a shared queue would reach only one worker.
    if work_topic:
        work_queue = await channel.declare_queue(work_topic, durable=True)
        await work_queue.bind(exch, routing_key=work_topic)
        queues.append(work_queue)

    outbox = outbox_topic or f"{agent_id}.output"
    waiting_key = f"{agent_id}.waiting"

    logger = logging.getLogger(__name__)
    logger.info("Worker %s ready", agent_id)
    logger.info("Inbox: %s + *.broadcast", inbox_topic)
    if work_topic:
        logger.info("Work queue (shared pool): %s", work_topic)
    logger.info("Outbox: %s | Idle timeout: %ss", outbox, idle_timeout)
    logger.info("Agent command: %s", " ".join(command))
    if not log_content:
        logger.info("Content logging off: task/output/stderr text is not logged")

    async def publish(routing_key: str, obj: dict) -> None:
        await exch.publish(
            aio_pika.Message(
                body=json.dumps(obj).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
        )

    async def run_task(task_text: str, task_id: str | None) -> tuple[int, int, int]:
        """Spawn the agent fresh, feed it the task on stdin, republish stdout.

        Returns ``(exit code, stdout bytes, stderr bytes)``.
        """
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

        counts = {"stdout": 0, "stderr": 0}

        async def pump_stdout() -> None:
            async for line in proc.stdout:
                counts["stdout"] += len(line)
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                message = {
                    "agent_id": agent_id,
                    "routing_key": outbox,
                    "type": "output",
                    "body": text,
                }
                if task_id is not None:
                    message["task_id"] = task_id
                await publish(outbox, message)
                if log_content:
                    logger.info("→ %s: %s", outbox, text[:80])

        async def pump_stderr() -> None:
            async for line in proc.stderr:
                counts["stderr"] += len(line)
                # Forward to real stderr so humans/journald can observe — only
                # when content logging is on, since it may echo task text.
                if log_content:
                    sys.stderr.write(line.decode(errors="replace"))
                    sys.stderr.flush()

        await asyncio.gather(pump_stdout(), pump_stderr())
        return await proc.wait(), counts["stdout"], counts["stderr"]

    last_activity = time.monotonic()
    idle_announced = False
    stop = False

    while not stop:
        # basic_get polling (not an async iterator): a timed-out iterator read
        # corrupts aio_pika's queue iterator, so we poll instead. Poll the inbox
        # first (directed messages win), then the shared pool.
        message = None
        for queue in queues:
            message = await queue.get(fail=False)
            if message is not None:
                break
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
                task_id = payload.get("task_id")
                if log_content:
                    logger.info("← task %s: %s", task_id, task[:80])
                else:
                    logger.info("← task %s (%d bytes)", task_id, len(task.encode()))
                rc, out_bytes, err_bytes = await run_task(task, task_id)
                logger.info(
                    "Task %s complete (rc=%d, stdout=%d bytes, stderr=%d bytes)",
                    task_id, rc, out_bytes, err_bytes,
                )
                # Explicit end-of-task marker, so a collector knows the task is
                # finished without waiting for the idle timeout or relying on the
                # agent to print a marker string.
                done = {
                    "agent_id": agent_id,
                    "routing_key": outbox,
                    "type": "done",
                    "rc": rc,
                }
                if task_id is not None:
                    done["task_id"] = task_id
                await publish(outbox, done)

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
    url_source = parser.add_mutually_exclusive_group()
    url_source.add_argument(
        "--amqp-url",
        default=None,
        help="RabbitMQ AMQP URL. Visible in the process list — prefer "
             "--amqp-url-file or $AMQP_URL when the URL carries a password.",
    )
    url_source.add_argument(
        "--amqp-url-file",
        default=None,
        metavar="PATH",
        help="Read the AMQP URL from this file (first line).",
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
        "--work-queue",
        default=None,
        metavar="NAME",
        help="Optional shared queue for a worker pool. Workers started with the "
             "same --work-queue compete for tasks published to that routing key "
             "(the broker load-balances). The per-agent --inbox still receives "
             "directed messages.",
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
        "--log-content",
        action="store_true",
        help="Log task text, agent output and agent stderr (off by default: only "
             "task ids, byte counts and exit codes are logged).",
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

    try:
        amqp_url = resolve_amqp_url(args.amqp_url, args.amqp_url_file, os.environ)
    except ValueError as exc:
        parser.error(str(exc))

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
        amqp_url=amqp_url,
        exchange=args.exchange,
        inbox_topic=args.inbox,
        work_topic=args.work_queue,
        outbox_topic=args.outbox,
        idle_timeout=args.idle_timeout,
        preamble=preamble,
        command=args.command,
        log_content=args.log_content,
    ))


if __name__ == "__main__":
    main()
