# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""One task through the whole bridge, with a fake broker and a fake agent.

The broker is replaced by objects that record what was published; the agent is
a short python process that replays the stream-json fixture. What is being
tested is the wiring the unit tests cannot see: which routing keys get which
messages, what lands on `done`, and what the ledger ends up holding.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

from agentbridge import bridge

FIXTURE = Path(__file__).parent / "fixtures" / "stream-json.jsonl"


class FakeMessage:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode()

    @asynccontextmanager
    async def process(self):
        yield


class FakeQueue:
    def __init__(self, messages=()):
        self._messages = list(messages)

    async def bind(self, *a, **kw):
        return None

    async def get(self, fail=False):
        return self._messages.pop(0) if self._messages else None


class FakeExchange:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, message, routing_key):
        self.published.append((routing_key, json.loads(message.body.decode())))


class FakeChannel:
    def __init__(self, exchange, queue):
        self._exchange = exchange
        self._queue = queue

    async def set_qos(self, **kw):
        return None

    async def declare_exchange(self, *a, **kw):
        return self._exchange

    async def declare_queue(self, *a, **kw):
        return self._queue


class FakeConnection:
    def __init__(self, channel):
        self._channel = channel

    async def channel(self):
        return self._channel

    async def close(self):
        return None


def agent_replaying(path: Path) -> list[str]:
    """An agent that consumes its prompt and replays a recorded stream."""
    script = (f"import sys; sys.stdin.read(); "
              f"sys.stdout.write(open({str(path)!r}).read())")
    return [sys.executable, "-c", script]


class TaskRun(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = Path(self._tmp.name) / "alice.ledger.jsonl"
        self.exchange = FakeExchange()

    async def run_one(self, command, driver, task=None, **kw):
        queue = FakeQueue([
            FakeMessage(task or {"body": "summarise the file", "task_id": "T1"}),
            FakeMessage({"type": "shutdown"}),
        ])
        channel = FakeChannel(self.exchange, queue)
        with mock.patch.object(
            bridge.aio_pika, "connect_robust",
            new=mock.AsyncMock(return_value=FakeConnection(channel)),
        ):
            await asyncio.wait_for(bridge.bridge(
                agent_id="researcher-1",
                amqp_url="amqp://fake/",
                exchange="agents",
                inbox_topic="researcher-1.inbox",
                work_topic=None,
                outbox_topic=None,
                idle_timeout=600.0,
                preamble="PREAMBLE",
                command=command,
                driver_name=driver,
                ledger_file=str(self.ledger_path),
                role="researcher",
                **kw,
            ), timeout=60)

    def sent(self, routing_key=None, kind=None):
        return [body for key, body in self.exchange.published
                if (routing_key is None or key == routing_key)
                and (kind is None or body.get("type") == kind)]

    def ledger_lines(self):
        return [json.loads(ln)
                for ln in self.ledger_path.read_text().splitlines()]


class StreamJsonTask(TaskRun):
    async def asyncSetUp(self):
        await self.run_one(agent_replaying(FIXTURE), "claude-stream-json")

    def test_the_answer_reaches_the_outbox_once(self):
        outputs = self.sent("researcher-1.output", "output")
        self.assertEqual([o["body"] for o in outputs],
                         ["The file contains: hello from the fixture"])
        self.assertEqual(outputs[0]["task_id"], "T1")

    def test_start_and_progress_go_to_the_progress_key_only(self):
        progress = self.sent("researcher-1.progress")
        self.assertEqual(progress[0]["type"], "start")
        self.assertEqual(progress[0]["task_id"], "T1")
        self.assertTrue(any(p.get("tool") == "Read" for p in progress))
        # Nothing of this traffic is on the outbox, so `collect` is unchanged.
        self.assertEqual(self.sent("researcher-1.output", "progress"), [])
        self.assertEqual(self.sent("researcher-1.output", "start"), [])

    def test_progress_is_throttled_to_one_per_interval(self):
        # The fixture has one tool call and two bare assistant turns; the
        # throttle lets the first through and suppresses the rest.
        progress = [p for p in self.sent("researcher-1.progress")
                    if p["type"] == "progress"]
        self.assertEqual(len(progress), 1)

    def test_done_carries_metrics(self):
        done = self.sent("researcher-1.output", "done")[0]
        self.assertEqual(done["rc"], 0)
        self.assertEqual(done["metrics"]["model"], "claude-haiku-5-5")
        self.assertAlmostEqual(done["metrics"]["cost_usd"], 0.00306154)
        self.assertNotIn("driver_error", done)

    def test_the_ledger_gets_one_metadata_line(self):
        lines = self.ledger_lines()
        self.assertEqual(len(lines), 1)
        row = lines[0]
        self.assertEqual(row["task_id"], "T1")
        self.assertEqual(row["worker"], "researcher-1")
        self.assertEqual(row["role"], "researcher")
        self.assertEqual(row["backend"], "anthropic")
        self.assertEqual(row["driver"], "claude-stream-json")
        self.assertEqual(row["kind"], "task")
        self.assertEqual(row["rc"], 0)
        self.assertEqual(row["model"], "claude-haiku-5-5")
        self.assertEqual(row["num_turns"], 3)
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertEqual(row["agent_duration_ms"], 2567)

    def test_the_ledger_holds_no_text(self):
        blob = self.ledger_path.read_text()
        for secret in ("summarise the file", "PREAMBLE", "hello from the fixture",
                       "SECRET-THINKING-MUST-NOT-LEAK", "INTERMEDIATE-TEXT"):
            self.assertNotIn(secret, blob)

    def test_the_ledger_is_private(self):
        self.assertEqual(self.ledger_path.stat().st_mode & 0o777, 0o600)


class TextTask(TaskRun):
    async def test_the_text_driver_still_publishes_every_line(self):
        script = (r"import sys; sys.stdin.read(); "
                  r"print('line one'); print('line two')")
        await self.run_one([sys.executable, "-c", script], "claude-text")
        outputs = self.sent("researcher-1.output", "output")
        self.assertEqual([o["body"] for o in outputs], ["line one", "line two"])
        done = self.sent("researcher-1.output", "done")[0]
        self.assertIsNone(done["metrics"])

    async def test_a_ledger_line_is_written_even_without_metrics(self):
        await self.run_one([sys.executable, "-c", "import sys; sys.stdin.read()"],
                           "claude-text")
        row = self.ledger_lines()[0]
        self.assertEqual(row["rc"], 0)
        self.assertNotIn("cost_usd", row)


class Failures(TaskRun):
    async def test_a_nonzero_exit_is_recorded(self):
        await self.run_one(
            [sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(3)"],
            "claude-stream-json")
        done = self.sent("researcher-1.output", "done")[0]
        self.assertEqual(done["rc"], 3)
        self.assertIsNone(done["metrics"])
        self.assertIn("without a result event", done["driver_error"])
        row = self.ledger_lines()[0]
        self.assertEqual(row["rc"], 3)
        self.assertIn("without a result event", row["driver_error"])

    async def test_a_refused_task_is_not_run_and_not_recorded(self):
        await self.run_one(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            "claude-stream-json",
            task={"body": "x", "task_id": "T9", "model": "../../etc/passwd"},
        )
        outputs = self.sent("researcher-1.output", "output")
        self.assertIn("task rejected", outputs[0]["body"])
        # A task that never ran has nothing to measure.
        self.assertFalse(self.ledger_path.exists())
        self.assertEqual(self.sent("researcher-1.output", "done")[0]["rc"], 2)

    async def test_a_broken_ledger_path_does_not_fail_the_task(self):
        self.ledger_path = Path(self._tmp.name) / "no-such-dir" / "l.jsonl"
        await self.run_one(agent_replaying(FIXTURE), "claude-stream-json")
        # The answer still got through.
        self.assertTrue(self.sent("researcher-1.output", "output"))
        self.assertEqual(self.sent("researcher-1.output", "done")[0]["rc"], 0)


if __name__ == "__main__":
    unittest.main()
