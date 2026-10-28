# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The fleet view: merging two sources of truth, and the tap's reduction."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge import status
from agentbridge.service import config, fleet, rabbit_mgmt, tap
from agentbridge.service.tenant import tenant_from_headers


def unit(agent_id, active="active", sub="running"):
    return {"id": agent_id, "unit": f"agentbridge-alice-{agent_id}.service",
            "active": active, "sub": sub}


class Merge(unittest.TestCase):
    def test_a_worker_with_both_sources_keeps_its_detail(self):
        rows = fleet.merge(
            [{"agent": "r-1", "state": "working", "task_id": "T1",
              "last_tool": "Bash"}],
            [unit("r-1")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "working")
        self.assertEqual(rows[0]["task_id"], "T1")
        self.assertEqual(rows[0]["unit_active"], "active")

    def test_systemd_wins_on_whether_a_worker_exists(self):
        # A killed worker leaves a file claiming it is still working.
        rows = fleet.merge(
            [{"agent": "r-1", "state": "working", "task_id": "T1"}],
            [unit("r-1", active="inactive", sub="dead")])
        self.assertEqual(rows[0]["state"], fleet.GONE)

    def test_a_failed_unit_is_reported_as_failed(self):
        rows = fleet.merge([{"agent": "r-1", "state": "idle"}],
                           [unit("r-1", active="failed", sub="failed")])
        self.assertEqual(rows[0]["state"], fleet.FAILED)

    def test_activating_counts_as_alive(self):
        rows = fleet.merge([], [unit("r-1", active="activating", sub="start")])
        self.assertEqual(rows[0]["state"], "starting")

    def test_a_unit_with_no_status_file_is_still_shown(self):
        rows = fleet.merge([], [unit("r-1")])
        self.assertEqual(rows[0]["agent"], "r-1")
        self.assertEqual(rows[0]["state"], "starting")

    def test_a_status_file_with_no_unit_is_shown_as_gone(self):
        # Visible rather than silently dropped: a worker that died while
        # working is exactly what an operator needs to see.
        rows = fleet.merge([{"agent": "ghost-9", "state": "working"}], [])
        self.assertEqual(rows[0]["state"], fleet.GONE)
        self.assertIsNone(rows[0]["unit_active"])

    def test_rows_are_sorted_by_agent(self):
        rows = fleet.merge([], [unit("r-2"), unit("i-1")])
        self.assertEqual([r["agent"] for r in rows], ["i-1", "r-2"])


class TenantFleet(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for patch in (
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "WORKSPACES_DIR", root / "ws"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config.TENANTS_DIR.mkdir()
        (config.TENANTS_DIR / "alice.json").write_text(json.dumps({
            "vhost": "ab-alice", "bus_user": "ab-alice",
            "bus_password": "p", "limits": {"max_workers": 4},
        }))
        self.alice = tenant_from_headers({config.USER_HEADER: "alice"})

    def write_status(self, agent_id, **fields):
        writer = status.StatusWriter(
            fleet.status_path(self.alice, agent_id), agent=agent_id,
            role="researcher")
        writer.set(fields.pop("state", "idle"), **fields)

    def test_the_status_dir_is_outside_the_workspace(self):
        self.assertNotIn(str(config.WORKSPACES_DIR), str(self.alice.status_dir))
        self.assertTrue(str(self.alice.status_dir).startswith(
            str(config.TENANTS_DIR)))

    def test_sweep_removes_only_files_whose_unit_is_gone(self):
        self.write_status("r-1")
        self.write_status("ghost-9")
        removed = fleet.sweep(self.alice, [unit("r-1")])
        self.assertEqual(removed, ["ghost-9"])
        self.assertTrue(fleet.status_path(self.alice, "r-1").exists())
        self.assertFalse(fleet.status_path(self.alice, "ghost-9").exists())

    def test_snapshot_shape_and_totals(self):
        self.write_status("r-1", state="working", task_id="T1")
        self.write_status("r-2", state="idle")
        with mock.patch.object(config, "RABBITMQ_ADMIN_USER", ""):
            snap = asyncio.run(fleet.snapshot(
                self.alice, [unit("r-1"), unit("r-2")]))
        self.assertEqual(snap["user"], "alice")
        self.assertEqual(snap["totals"]["workers"], 2)
        self.assertEqual(snap["totals"]["by_state"], {"working": 1, "idle": 1})
        self.assertEqual(snap["totals"]["max_workers"], 4)
        self.assertEqual(snap["totals"]["usage_7d"]["tasks"], 0)
        # Omitted entirely rather than reported as empty: no API, no answer.
        self.assertNotIn("queues", snap)

    def test_queues_appear_when_the_management_api_is_configured(self):
        rows = [{"name": "r-1.inbox", "ready": 2, "unacked": 1, "consumers": 1}]
        with mock.patch.object(config, "RABBITMQ_ADMIN_USER", "admin"), \
             mock.patch.object(rabbit_mgmt, "queues", return_value=rows):
            snap = asyncio.run(fleet.snapshot(self.alice, [unit("r-1")]))
        self.assertEqual(snap["queues"], rows)

    def test_a_broken_management_api_degrades_instead_of_failing(self):
        with mock.patch.object(config, "RABBITMQ_ADMIN_USER", "admin"), \
             mock.patch.object(rabbit_mgmt, "queues",
                               side_effect=rabbit_mgmt.RabbitError("down")):
            snap = asyncio.run(fleet.snapshot(self.alice, [unit("r-1")]))
        self.assertNotIn("queues", snap)
        self.assertEqual(snap["totals"]["workers"], 1)

    def test_the_snapshot_carries_no_secret(self):
        self.write_status("r-1")
        with mock.patch.object(config, "RABBITMQ_ADMIN_USER", ""):
            snap = asyncio.run(fleet.snapshot(self.alice, [unit("r-1")]))
        blob = json.dumps(snap)
        self.assertNotIn("bus_password", blob)
        self.assertNotIn("amqp://", blob)


OUTPUT_EVENT = {"agent_id": "r-1", "task_id": "T1", "type": "output",
                "body": "<img src=x onerror=alert(1)>",
                "routing_key": "r-1.output"}


class TapReduction(unittest.TestCase):
    """Bodies are dropped here, in the service, not in the browser."""

    OUTPUT = OUTPUT_EVENT

    def test_a_body_is_dropped_by_default(self):
        event = tap.reduce_event(self.OUTPUT)
        self.assertNotIn("body", event)
        self.assertEqual(event["agent_id"], "r-1")
        self.assertEqual(event["type"], "output")

    def test_a_body_is_kept_only_when_asked_for(self):
        event = tap.reduce_event(self.OUTPUT, bodies=True)
        # Kept verbatim as a JSON string; the page renders it as text.
        self.assertEqual(event["body"], "<img src=x onerror=alert(1)>")

    def test_a_long_body_is_truncated_and_flagged(self):
        payload = dict(self.OUTPUT, body="x" * (tap.BODY_LIMIT + 500))
        event = tap.reduce_event(payload, bodies=True)
        self.assertEqual(len(event["body"]), tap.BODY_LIMIT)
        self.assertTrue(event["truncated"])

    def test_unknown_fields_never_reach_the_browser(self):
        payload = dict(self.OUTPUT, bus_password="p", amqp_url="amqp://x/",
                       routing_key="r-1.output")
        event = tap.reduce_event(payload, bodies=True)
        self.assertNotIn("bus_password", event)
        self.assertNotIn("amqp_url", event)
        self.assertNotIn("routing_key", event)
        self.assertLessEqual(set(event), set(tap.FIELDS) | {"body", "truncated"})

    def test_metrics_and_rc_pass_through(self):
        event = tap.reduce_event({"type": "done", "rc": 0, "task_id": "T1",
                                  "metrics": {"cost_usd": 0.01}})
        self.assertEqual(event["rc"], 0)
        self.assertEqual(event["metrics"]["cost_usd"], 0.01)


class TapSlots(unittest.TestCase):
    def setUp(self):
        tap._connections.clear()
        self.addCleanup(tap._connections.clear)

    def test_three_concurrent_views_are_allowed_and_a_fourth_is_refused(self):
        tenant = mock.Mock(name="alice")
        tenant.name = "alice"

        async def scenario():
            async with (tap.connection_slot(tenant), tap.connection_slot(tenant),
                        tap.connection_slot(tenant)):
                with self.assertRaises(tap.TapBusy):
                    async with tap.connection_slot(tenant):
                        pass
            # All released again.
            self.assertEqual(tap._connections.get("alice"), None)

        asyncio.run(scenario())

    def test_a_slot_is_released_even_if_the_stream_raises(self):
        tenant = mock.Mock()
        tenant.name = "alice"

        async def scenario():
            with self.assertRaises(RuntimeError):
                async with tap.connection_slot(tenant):
                    raise RuntimeError("browser went away")
            self.assertIsNone(tap._connections.get("alice"))

        asyncio.run(scenario())

    def test_tenants_have_separate_budgets(self):
        a, b = mock.Mock(), mock.Mock()
        a.name, b.name = "alice", "bob"

        async def scenario():
            async with (tap.connection_slot(a), tap.connection_slot(a),
                        tap.connection_slot(a), tap.connection_slot(b)):
                pass                                    # bob is unaffected

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
