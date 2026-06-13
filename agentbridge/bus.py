# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator-side RabbitMQ helper for agentbridge.

A small CLI the orchestrator uses to drive the bus: declare its collector queue,
send tasks/control messages to workers, and collect their output. Agents never
use this — they only speak to their bridge over stdio. The AMQP URL comes from
``--amqp-url`` or the ``$AMQP_URL`` environment variable.

    agentbus setup --purge
    agentbus send --to research.work --body "Summarise paper X"
    agentbus send --to research-1.inbox --shutdown
    agentbus collect --idle 30 --until TASK_COMPLETE
"""

import argparse
import asyncio
import json
import os
import sys
import time

import aio_pika

DEFAULT_EXCHANGE = "agents"
COLLECTOR_QUEUE = "orchestrator.collect"


def _resolve_amqp(args: argparse.Namespace) -> str:
    url = args.amqp_url or os.environ.get("AMQP_URL")
    if not url:
        sys.exit("agentbus: no AMQP URL — pass --amqp-url or set $AMQP_URL")
    return url


async def _connect(args: argparse.Namespace):
    conn = await asyncio.wait_for(aio_pika.connect(_resolve_amqp(args)), timeout=15)
    channel = await conn.channel()
    exch = await channel.declare_exchange(
        args.exchange, aio_pika.ExchangeType.TOPIC, durable=True
    )
    return conn, channel, exch


async def _declare_collector(channel, exch):
    """Declare the durable collector queue bound to every agent's output/idle."""
    queue = await channel.declare_queue(COLLECTOR_QUEUE, durable=True)
    await queue.bind(exch, routing_key="*.output")
    await queue.bind(exch, routing_key="*.waiting")
    return queue


async def cmd_setup(args: argparse.Namespace) -> None:
    conn, channel, exch = await _connect(args)
    queue = await _declare_collector(channel, exch)
    if args.purge:
        await queue.purge()
    print(f"collector '{COLLECTOR_QUEUE}' bound to *.output / *.waiting"
          + (" (purged)" if args.purge else ""))
    await conn.close()


async def cmd_send(args: argparse.Namespace) -> None:
    conn, channel, exch = await _connect(args)
    # Ensure the destination queue exists and is bound before publishing, so a
    # task isn't dropped by the topic exchange when it races a worker's own
    # startup declaration. Our convention is queue name == routing key for
    # directed inboxes (`{agent}.inbox`) and pools (`{role}.work`); the worker
    # re-declares the same durable queue idempotently. Broadcasts (`*.broadcast`)
    # rely on existing inbox bindings, so don't manufacture a queue for them.
    if not args.to.endswith(".broadcast"):
        queue = await channel.declare_queue(args.to, durable=True)
        await queue.bind(exch, routing_key=args.to)
    if args.shutdown:
        message = {"type": "shutdown"}
    elif args.json is not None:
        message = json.loads(args.json)
    else:
        message = {"body": args.body}
    await exch.publish(
        aio_pika.Message(
            body=json.dumps(message).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=args.to,
    )
    print(f"sent -> {args.to}: {message.get('type', 'task')} "
          f"{str(message.get('body', '')).strip()[:60]}")
    await conn.close()


async def cmd_collect(args: argparse.Namespace) -> None:
    conn, channel, exch = await _connect(args)
    queue = await _declare_collector(channel, exch)
    start = last = time.monotonic()
    # basic_get polling: a timed-out async-iterator read corrupts aio_pika's
    # queue iterator, so we poll instead.
    while True:
        message = await queue.get(fail=False)
        if message is None:
            if time.monotonic() - last > args.idle:
                print(f"[collect] idle {args.idle:g}s — stop")
                break
            if time.monotonic() - start > args.wall:
                print(f"[collect] wall {args.wall:g}s — stop")
                break
            await asyncio.sleep(1.0)
            continue
        async with message.process():
            last = time.monotonic()
            try:
                payload = json.loads(message.body.decode())
            except json.JSONDecodeError:
                payload = {"body": message.body.decode(errors="replace")}
            tag = "WAIT" if payload.get("type") == "waiting_for_input" else "OUT"
            line = payload.get("body", "")
            print(f"[{tag}|{payload.get('agent_id', '?')}] {line}")
            if args.until and args.until in line:
                print("[collect] matched --until — stop")
                break
    await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agentbus",
        description="Orchestrator-side bus helper for agentbridge",
    )
    parser.add_argument("--amqp-url", default=None,
                        help="RabbitMQ AMQP URL (default: $AMQP_URL env var)")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE,
                        help=f"Exchange name (default: {DEFAULT_EXCHANGE})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="declare + bind the collector queue")
    p_setup.add_argument("--purge", action="store_true",
                         help="purge stale messages from the collector")
    p_setup.set_defaults(func=cmd_setup)

    p_send = sub.add_parser("send", help="publish a task or control message")
    p_send.add_argument("--to", required=True, metavar="ROUTING_KEY",
                        help="e.g. research-1.inbox or research.work")
    group = p_send.add_mutually_exclusive_group(required=True)
    group.add_argument("--body", help='task text -> {"body": ...}')
    group.add_argument("--json", help="raw JSON message body")
    group.add_argument("--shutdown", action="store_true",
                       help='send {"type": "shutdown"}')
    p_send.set_defaults(func=cmd_send)

    p_collect = sub.add_parser("collect", help="consume + print agent output")
    p_collect.add_argument("--idle", type=float, default=30.0,
                           help="stop after N seconds with no message (default 30)")
    p_collect.add_argument("--wall", type=float, default=300.0,
                           help="stop after N seconds total (default 300)")
    p_collect.add_argument("--until", default=None,
                           help="stop when an output line contains this substring")
    p_collect.set_defaults(func=cmd_collect)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
