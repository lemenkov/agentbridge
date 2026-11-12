# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""`collect` must still return while a worker sits idle.

The bridge repeats its `waiting` announcement every 30s so the message can
carry a TTL and not outlive its worker (row 11d). That repeat is only safe
because the collector does not treat an idle announcement as activity: an
announcement that nothing is happening must not extend the window that waits
for something to happen, or `collect` would never return while any worker was
free.
"""

import asyncio
import json
import types
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from agentbridge.service import broker


class FakeMessage:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    @asynccontextmanager
    async def process(self):
        yield


class Heartbeating:
    """Publishes a `waiting` every `period` of fake time, nothing in between.

    It drives the clock itself: every poll advances fake time by `step`, which
    makes the test deterministic rather than a race against a real timer.
    """

    def __init__(self, clock, step=5.0, period=10.0):
        self._clock = clock
        self._step = step
        self._period = period
        self._last_beat = 0.0

    async def get(self, fail=False):
        self._clock.t += self._step
        if self._clock.t - self._last_beat >= self._period:
            self._last_beat = self._clock.t
            return FakeMessage({"agent_id": "researcher-1",
                                "type": "waiting_for_input", "body": "idle"})
        return None


class DrainWithAnIdleWorker(unittest.IsolatedAsyncioTestCase):
    async def drain(self, **kw):
        clock = types.SimpleNamespace(t=0.0)
        queue = Heartbeating(clock)

        @asynccontextmanager
        async def fake_channel(_tenant):
            yield (None, None)

        with mock.patch.object(broker, "time", types.SimpleNamespace(
                    monotonic=lambda: clock.t)), \
             mock.patch.object(broker, "_channel", fake_channel), \
             mock.patch.object(broker, "_declare_collector",
                               new=mock.AsyncMock(return_value=queue)), \
             mock.patch.object(broker.asyncio, "sleep",
                               new=mock.AsyncMock(return_value=None)):
            return await asyncio.wait_for(broker.drain(
                tenant=mock.Mock(), idle=20.0, max_seconds=10_000.0,
                max_messages=1000, until=None, done=0, **kw), timeout=30)

    async def test_it_stops_on_idle_despite_the_heartbeats(self):
        # Beats arrive every 10s of fake time against a 20s idle window. If an
        # announcement reset the timer, this would run to max_seconds instead.
        _, stopped_by = await self.drain()
        self.assertEqual(stopped_by, "idle")

    async def test_the_announcement_is_still_reported(self):
        # Not resetting the timer must not mean dropping the message: an idle
        # worker is exactly what the orchestrator needs to hear about.
        received, _ = await self.drain()
        self.assertTrue(received)
        self.assertEqual({m["type"] for m in received}, {"waiting_for_input"})
        self.assertEqual({m["agent_id"] for m in received}, {"researcher-1"})


if __name__ == "__main__":
    unittest.main()
