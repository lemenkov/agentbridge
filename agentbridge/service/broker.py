# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Bus operations inside one tenant's vhost. The connection uses the tenant's
own bus user, which RabbitMQ only permits on that vhost — the broker, not this
code, enforces the boundary between tenants."""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

import aio_pika

from .. import status
from ..bus import _declare_collector
from . import config
from .tenant import Tenant


@asynccontextmanager
async def _channel(tenant: Tenant):
    conn = await asyncio.wait_for(aio_pika.connect(tenant.amqp_url), timeout=15)
    try:
        channel = await conn.channel()
        exch = await channel.declare_exchange(
            config.EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )
        yield channel, exch
    finally:
        await conn.close()


async def ensure_collector(tenant: Tenant) -> None:
    """Declare the tenant's collector so worker output is captured from the start."""
    async with _channel(tenant) as (channel, exch):
        await _declare_collector(channel, exch)


async def publish(tenant: Tenant, routing_key: str, message: dict) -> None:
    """Publish one message. Like ``agentbus send``, declare the destination
    queue first (except for broadcasts) so it can't race a worker's startup."""
    async with _channel(tenant) as (channel, exch):
        if not routing_key.endswith(".broadcast"):
            queue = await channel.declare_queue(routing_key, durable=True)
            await queue.bind(exch, routing_key=routing_key)
        await exch.publish(
            aio_pika.Message(
                body=json.dumps(message).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
        )


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


async def publish_dispatch(tenant: Tenant, task_id: str, to: str) -> None:
    """Announce that a task was sent, for the fleet page's timeline.

    It goes to ``orchestrator.dispatch``, which the collector's bindings do
    not match, so this never shows up in ``collect``. It carries no body: the
    page needs to know that a task was dispatched and where, not what it said.
    A failure here must not fail the send that caused it — the task is already
    on its way, and a missing tick on a page is not worth an error.
    """
    try:
        async with _channel(tenant) as (_, exch):
            await exch.publish(
                aio_pika.Message(
                    body=json.dumps({
                        "type": "dispatch", "task_id": task_id, "to": to,
                        "ts": status.now_iso(),
                    }).encode(),
                    # Nothing durable about a UI event.
                    delivery_mode=aio_pika.DeliveryMode.NOT_PERSISTENT,
                ),
                routing_key="orchestrator.dispatch",
            )
    except (OSError, aio_pika.exceptions.AMQPError, asyncio.TimeoutError):
        pass


async def drain(
    tenant: Tenant,
    idle: float,
    max_seconds: float,
    max_messages: int,
    until: str | None,
    done: int,
) -> tuple[list[dict], str]:
    """Consume the tenant's collector until a stop condition; return the raw
    payloads and which condition stopped it."""
    received: list[dict] = []
    done_count = 0
    async with _channel(tenant) as (channel, exch):
        queue = await _declare_collector(channel, exch)
        start = last = time.monotonic()
        while True:
            message = await queue.get(fail=False)
            if message is None:
                now = time.monotonic()
                if now - last > idle:
                    return received, "idle"
                if now - start > max_seconds:
                    return received, "max_seconds"
                await asyncio.sleep(0.5)
                continue
            async with message.process():
                last = time.monotonic()
                try:
                    payload = json.loads(message.body.decode())
                except json.JSONDecodeError:
                    payload = {"body": message.body.decode(errors="replace")}
                received.append(payload)
            if payload.get("type") == "done":
                done_count += 1
                if done and done_count >= done:
                    return received, "done"
            elif until and until in str(payload.get("body", "")):
                return received, "until"
            if len(received) >= max_messages:
                return received, "max_messages"
