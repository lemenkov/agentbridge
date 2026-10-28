# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""An event tap: a copy of the bus traffic for the live page.

A topic exchange copies each message to *every* queue whose binding matches,
so a second queue beside the collector is a copy, never theft — ``collect``
loses nothing while a page is open. The tap queue is exclusive, auto-delete
and non-durable, with a TTL and a length cap, so a closed browser leaves no
queue behind and an abandoned one cannot grow without bound.

Events are reduced to metadata **here**, before they leave the service. Agent
output is untrusted — it is whatever a repository or a web page told an agent
to say — so bodies are dropped unless the viewer explicitly asks for them, and
then truncated. What reaches the browser is a JSON string either way; the page
renders it with ``textContent`` and never as markup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aio_pika

from . import config
from .tenant import Tenant

log = logging.getLogger("mcp-agentbridge")

# One per open page. A handful of tabs is reasonable; a hundred is a leak.
MAX_CONNECTIONS_PER_TENANT = 3

BINDINGS = ("*.output", "*.waiting", "*.progress", "orchestrator.dispatch")

QUEUE_ARGUMENTS = {
    # An unconsumed event is worthless after a minute.
    "x-message-ttl": 60_000,
    "x-max-length": 1_000,
    "x-overflow": "drop-head",
}

BODY_LIMIT = 2048

# Which fields of each event kind the page is allowed to see. Anything not
# listed here never reaches a browser, so a field added to a bus message
# later cannot leak by default.
FIELDS = ("type", "agent_id", "task_id", "tool", "phase", "rc", "ts",
          "to", "metrics", "driver_error")

_connections: dict[str, int] = {}


class TapBusy(Exception):
    """Too many pages open for this tenant."""


def reduce_event(payload: dict, *, bodies: bool = False) -> dict:
    """Keep the metadata, drop or truncate the text."""
    event = {k: payload[k] for k in FIELDS if k in payload}
    event.setdefault("type", "output")
    if bodies:
        body = payload.get("body")
        if isinstance(body, str) and body:
            event["body"] = body[:BODY_LIMIT]
            if len(body) > BODY_LIMIT:
                event["truncated"] = True
    return event


@asynccontextmanager
async def connection_slot(tenant: Tenant) -> AsyncIterator[None]:
    """Refuse a fourth concurrent page rather than open an unbounded number."""
    count = _connections.get(tenant.name, 0)
    if count >= MAX_CONNECTIONS_PER_TENANT:
        raise TapBusy(
            f"too many live views open ({count}); close one and retry"
        )
    _connections[tenant.name] = count + 1
    try:
        yield
    finally:
        remaining = _connections.get(tenant.name, 1) - 1
        if remaining <= 0:
            _connections.pop(tenant.name, None)
        else:
            _connections[tenant.name] = remaining


async def events(
    tenant: Tenant, *, bodies: bool = False, poll: float = 0.5,
    heartbeat: float | None = None,
) -> AsyncIterator[dict | None]:
    """Yield reduced events until the consumer stops iterating.

    Yields ``None`` as an idle tick every ``heartbeat`` seconds when no event
    has arrived. The tick belongs here rather than in the caller because the
    alternative — racing a timeout against ``__anext__`` — cancels this
    coroutine mid-poll and can drop an event that was already acknowledged.

    Polling with ``basic_get`` rather than a push consumer, for the same
    reason the bridge does: a timed-out async iterator read corrupts
    aio_pika's queue iterator.
    """
    conn = await asyncio.wait_for(aio_pika.connect(tenant.amqp_url), timeout=15)
    try:
        channel = await conn.channel()
        exch = await channel.declare_exchange(
            config.EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )
        queue = await channel.declare_queue(
            # Server-named, so two pages never collide.
            name="",
            durable=False,
            exclusive=True,
            auto_delete=True,
            arguments=dict(QUEUE_ARGUMENTS),
        )
        for binding in BINDINGS:
            await queue.bind(exch, routing_key=binding)
        log.info("user=%s tap=open bodies=%s", tenant.name, bodies)
        loop = asyncio.get_running_loop()
        last_tick = loop.time()
        while True:
            message = await queue.get(fail=False)
            if message is None:
                await asyncio.sleep(poll)
                if heartbeat and (loop.time() - last_tick) >= heartbeat:
                    last_tick = loop.time()
                    yield None
                continue
            last_tick = loop.time()
            async with message.process():
                try:
                    payload = json.loads(message.body.decode())
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield reduce_event(payload, bodies=bodies)
    finally:
        await conn.close()
        log.info("user=%s tap=closed", tenant.name)
