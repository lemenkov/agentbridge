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
import re
import sys
import time
import uuid
from collections.abc import Mapping

import aio_pika

from . import ledger
from .drivers import DEFAULT_DRIVER, DRIVERS, make_driver

# stream-json's init event enumerates the agent's tools and skills; it was
# 39 KB in a host capture, and asyncio's default 64 KiB stream limit would
# turn a longer one into a dead task. Read generously instead.
STREAM_LIMIT = 4 * 1024 * 1024

# At most one progress message per task per this many seconds (plan B.1): a
# tool-heavy task would otherwise publish hundreds, and progress exists to
# show liveness, not to be a transcript.
PROGRESS_INTERVAL = 5.0


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


# Per-task agent options, appended to the wrapped command as `--model X` /
# `--effort Y` (claude's flags). Values are allowlisted: a task sender must not
# be able to inject arbitrary arguments.
MODEL_RE = re.compile(r"^(fable|opus|sonnet|haiku|claude-[a-z0-9][a-z0-9.-]{0,63})$")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def validate_task_options(model: str | None, effort: str | None) -> dict[str, str]:
    """Return the validated ``{"model": ..., "effort": ...}`` subset that is set;
    raise ``ValueError`` naming the bad value otherwise."""
    options = {}
    if model is not None:
        if not isinstance(model, str) or not MODEL_RE.match(model):
            raise ValueError(
                f"invalid model {model!r}: use an alias (fable, opus, sonnet, "
                f"haiku) or a full model id such as claude-opus-5-5"
            )
        options["model"] = model
    if effort is not None:
        if effort not in EFFORT_LEVELS:
            raise ValueError(
                f"invalid effort {effort!r}: use one of {', '.join(EFFORT_LEVELS)}"
            )
        options["effort"] = effort
    return options


def option_args(options: dict[str, str]) -> list[str]:
    args = []
    for key in ("model", "effort"):
        if key in options:
            args += [f"--{key}", options[key]]
    return args


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
    default_options: dict[str, str] | None = None,
    allow_task_overrides: bool = False,
    driver_name: str = DEFAULT_DRIVER,
    ledger_file: str | None = None,
    role: str | None = None,
    backend: str = "anthropic",
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
    # Deliberately not bound to the collector's queue, so `collect` is
    # unchanged by this traffic; Part C's tap consumes it.
    progress_key = f"{agent_id}.progress"

    logger = logging.getLogger(__name__)
    logger.info("Worker %s ready", agent_id)
    logger.info("Inbox: %s + *.broadcast", inbox_topic)
    if work_topic:
        logger.info("Work queue (shared pool): %s", work_topic)
    logger.info("Outbox: %s | Idle timeout: %ss", outbox, idle_timeout)
    logger.info("Default options: %s | per-task overrides: %s",
                " ".join(option_args(default_options or {})) or "none",
                "allowed" if allow_task_overrides else "refused")
    logger.info("Driver: %s | role: %s | backend: %s",
                driver_name, role or "unset", backend)
    logger.info("Ledger: %s", ledger_file or "off")
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

    async def run_task(
        task_text: str, task_id: str | None, options: dict[str, str]
    ) -> dict:
        """Spawn the agent fresh, feed it the task on stdin, republish stdout.

        Returns ``{rc, stdout, stderr, metrics, driver_error, progress}``.
        """
        payload = f"{preamble}\n\nYour task: {task_text}" if preamble else task_text
        driver = make_driver(driver_name)

        proc = await asyncio.create_subprocess_exec(
            *command,
            *driver.extra_args,
            *option_args(options),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        proc.stdin.write(payload.encode())
        await proc.stdin.drain()
        proc.stdin.close()  # EOF so the agent starts processing the prompt

        counts = {"stdout": 0, "stderr": 0, "progress": 0, "overruns": 0}
        last_progress = 0.0

        async def emit(event) -> None:
            nonlocal last_progress
            if event.kind == "output":
                message = {
                    "agent_id": agent_id,
                    "routing_key": outbox,
                    "type": "output",
                    "body": event.body,
                }
                if task_id is not None:
                    message["task_id"] = task_id
                await publish(outbox, message)
                if log_content:
                    logger.info("→ %s: %s", outbox, (event.body or "")[:80])
                return
            # progress: throttled, and never carrying agent text.
            now = time.monotonic()
            if counts["progress"] and (now - last_progress) < PROGRESS_INTERVAL:
                return
            last_progress = now
            counts["progress"] += 1
            message = {
                "agent_id": agent_id,
                "routing_key": progress_key,
                "type": "progress",
                "ts": ledger.now_iso(),
            }
            if event.tool:
                message["tool"] = event.tool
            if event.phase:
                message["phase"] = event.phase
            if task_id is not None:
                message["task_id"] = task_id
            await publish(progress_key, message)

        async def pump_stdout() -> None:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:
                    # A single line longer than STREAM_LIMIT. Drop it rather
                    # than abandon the task, and let `done` report it.
                    counts["overruns"] += 1
                    continue
                if not line:
                    break
                counts["stdout"] += len(line)
                for event in driver.feed(line.decode(errors="replace")):
                    await emit(event)

        async def pump_stderr() -> None:
            while True:
                try:
                    line = await proc.stderr.readline()
                except ValueError:
                    counts["overruns"] += 1
                    continue
                if not line:
                    break
                counts["stderr"] += len(line)
                # Forward to real stderr so humans/journald can observe — only
                # when content logging is on, since it may echo task text.
                if log_content:
                    sys.stderr.write(line.decode(errors="replace"))
                    sys.stderr.flush()

        await asyncio.gather(pump_stdout(), pump_stderr())
        rc = await proc.wait()
        metrics, driver_error = driver.finish()
        if counts["overruns"]:
            driver_error = (
                f"{driver_error}; {counts['overruns']} over-long lines dropped"
                if driver_error
                else f"{counts['overruns']} over-long lines dropped"
            )
        return {
            "rc": rc,
            "stdout": counts["stdout"],
            "stderr": counts["stderr"],
            "progress": counts["progress"],
            "metrics": metrics,
            "driver_error": driver_error,
        }

    def record_task(
        task_id: str | None,
        options: dict[str, str],
        result: dict,
        ts_start: str,
        duration_ms: int,
    ) -> None:
        """Append one ledger line. A ledger failure must not fail the task."""
        if not ledger_file:
            return
        metrics = result.get("metrics") or {}
        record = {
            "kind": "task",
            "ts_start": ts_start,
            "ts_end": ledger.now_iso(),
            "task_id": task_id,
            "worker": agent_id,
            "role": role,
            "backend": backend,
            "driver": driver_name,
            "effort": options.get("effort"),
            "rc": result["rc"],
            # Wall clock as the worker saw it: it includes container start,
            # which the agent's own duration does not.
            "duration_ms": duration_ms,
            **{k: v for k, v in metrics.items() if k != "models"},
        }
        record.setdefault("model", options.get("model"))
        if metrics.get("models"):
            record["models"] = metrics["models"]
        if result.get("driver_error"):
            record["driver_error"] = result["driver_error"]
        try:
            ledger.append(ledger_file, {k: v for k, v in record.items()
                                        if v is not None})
        except OSError as exc:
            logger.warning("Ledger append failed for task %s: %s", task_id, exc)

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
                options = dict(default_options or {})
                error = None
                requested = {k: payload[k] for k in ("model", "effort") if k in payload}
                if requested and not allow_task_overrides:
                    error = "this worker does not accept per-task model/effort"
                elif requested:
                    try:
                        options.update(validate_task_options(
                            requested.get("model"), requested.get("effort")))
                    except ValueError as exc:
                        error = str(exc)

                result: dict | None = None
                if error is None:
                    ts_start = ledger.now_iso()
                    started = time.monotonic()
                    start_message = {
                        "agent_id": agent_id,
                        "routing_key": progress_key,
                        "type": "start",
                        "ts": ts_start,
                    }
                    if task_id is not None:
                        start_message["task_id"] = task_id
                    await publish(progress_key, start_message)

                    result = await run_task(task, task_id, options)
                    rc = result["rc"]
                    duration_ms = int((time.monotonic() - started) * 1000)
                    record_task(task_id, options, result, ts_start, duration_ms)
                    logger.info(
                        "Task %s complete (rc=%d, %s, stdout=%d bytes, "
                        "stderr=%d bytes, progress=%d, %.1fs)",
                        task_id, rc, " ".join(option_args(options)) or "default model",
                        result["stdout"], result["stderr"], result["progress"],
                        duration_ms / 1000,
                    )
                    if result["driver_error"]:
                        logger.warning("Task %s driver: %s",
                                       task_id, result["driver_error"])
                else:
                    # Refuse rather than run with options the sender didn't ask for.
                    rc = 2
                    logger.info("Task %s rejected: %s", task_id, error)
                    rejected = {
                        "agent_id": agent_id,
                        "routing_key": outbox,
                        "type": "output",
                        "body": f"[agentbridge] task rejected: {error}",
                    }
                    if task_id is not None:
                        rejected["task_id"] = task_id
                    await publish(outbox, rejected)
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
                if result is not None:
                    # Null rather than absent when the driver could not produce
                    # them, so a collector can tell "no metrics" from "metrics
                    # not supported".
                    done["metrics"] = result["metrics"]
                    if result["driver_error"]:
                        done["driver_error"] = result["driver_error"]
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
        "--model",
        default=None,
        help="Default model for every task, appended to the wrapped command as "
             "`--model MODEL` (an alias such as opus, or a full model id).",
    )
    parser.add_argument(
        "--effort",
        default=None,
        choices=EFFORT_LEVELS,
        help="Default effort for every task, appended as `--effort LEVEL`.",
    )
    parser.add_argument(
        "--allow-task-overrides",
        action="store_true",
        help='Let a task message set "model"/"effort" for that task, overriding '
             "the defaults (values are allowlisted).",
    )
    parser.add_argument(
        "--driver",
        default=DEFAULT_DRIVER,
        choices=sorted(DRIVERS),
        help="How to run and parse the agent (default: %(default)s). "
             "claude-stream-json adds --output-format stream-json --verbose to "
             "the wrapped command and yields tool progress and token/cost "
             "metrics; claude-text treats every stdout line as output and "
             "works with any command.",
    )
    parser.add_argument(
        "--ledger-file",
        default=None,
        metavar="PATH",
        help="Append one JSON metadata line per finished task (ids, model, "
             "counters, timings, cost — never task text or output). Keep it "
             "outside any workspace an agent can write to.",
    )
    parser.add_argument(
        "--role",
        default=None,
        help="Role name recorded in the ledger (not passed to the agent).",
    )
    parser.add_argument(
        "--backend",
        default="anthropic",
        help="Model backend recorded in the ledger (default: %(default)s).",
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
        default_options = validate_task_options(args.model, args.effort)
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

    parts = []
    for path in args.preamble_file:
        with open(path, encoding="utf-8") as fh:
            parts.append(fh.read())
    preamble = "\n\n".join(parts)

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
        default_options=default_options,
        allow_task_overrides=args.allow_task_overrides,
        driver_name=args.driver,
        ledger_file=args.ledger_file,
        role=args.role,
        backend=args.backend,
    ))


if __name__ == "__main__":
    main()
