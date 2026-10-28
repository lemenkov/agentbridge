# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The task ledger: appending, rotation, reading back and aggregation."""

import json
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentbridge import ledger


def stamp(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(
        timespec="seconds")


class LedgerFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "alice.ledger.jsonl"

    def write(self, count=1, **fields):
        for i in range(count):
            record = {"kind": "task", "task_id": f"t{i}", "rc": 0,
                      "ts_start": stamp(minutes=1), "ts_end": stamp(),
                      "duration_ms": 1000, "role": "researcher",
                      "model": "claude-haiku-5-5"}
            record.update(fields)
            ledger.append(self.path, record)


class Append(LedgerFile):
    def test_creates_the_file_0600(self):
        self.write()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_one_json_object_per_line(self):
        self.write(count=3)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([json.loads(ln)["task_id"] for ln in lines],
                         ["t0", "t1", "t2"])

    def test_rotates_at_the_size_limit_keeping_one_generation(self):
        rotated = self.path.with_name(self.path.name + ".1")
        written = 0
        with unittest.mock.patch.object(ledger, "MAX_BYTES", 500):
            while not rotated.exists():
                ledger.append(self.path, {"task_id": f"t{written}",
                                          "pad": "x" * 100})
                written += 1
        # Across one rotation nothing is lost: every record is in one file
        # or the other.
        total = (len(self.path.read_text().splitlines())
                 + len(rotated.read_text().splitlines()))
        self.assertEqual(total, written)

    def test_a_second_rotation_discards_the_oldest_generation(self):
        # Only one .1 is kept, so the ledger is a bounded window by design,
        # not a permanent archive. The newest records are the ones that stay.
        with unittest.mock.patch.object(ledger, "MAX_BYTES", 300):
            for i in range(40):
                ledger.append(self.path, {"task_id": f"t{i}", "ts_end": stamp(),
                                          "pad": "x" * 100})
        rows = ledger.records(self.path, limit=100)
        self.assertLess(len(rows), 40)
        self.assertEqual(rows[0]["task_id"], "t39")

    def test_concurrent_writers_do_not_interleave_lines(self):
        def worker(n):
            for i in range(25):
                ledger.append(self.path, {"worker": f"w{n}", "i": i,
                                          "pad": "x" * 200})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 100)
        # Every line is complete JSON — no half-written record.
        for line in lines:
            json.loads(line)


class Records(LedgerFile):
    def test_newest_first_and_limited(self):
        for i in range(5):
            ledger.append(self.path, {"task_id": f"t{i}", "ts_end": stamp()})
        rows = ledger.records(self.path, limit=2)
        self.assertEqual([r["task_id"] for r in rows], ["t4", "t3"])

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(ledger.records(self.path), [])

    def test_a_truncated_last_line_does_not_hide_the_history(self):
        self.write(count=2)
        with open(self.path, "a") as fh:
            fh.write('{"task_id": "part')      # killed mid-write
        self.assertEqual(len(ledger.records(self.path)), 2)

    def test_since_filters_by_timestamp(self):
        ledger.append(self.path, {"task_id": "old", "ts_end": stamp(days=9)})
        ledger.append(self.path, {"task_id": "new", "ts_end": stamp(hours=1)})
        rows = ledger.records(self.path, since="7d")
        self.assertEqual([r["task_id"] for r in rows], ["new"])

    def test_role_and_kind_filter(self):
        ledger.append(self.path, {"task_id": "r", "role": "reviewer",
                                  "ts_end": stamp()})
        ledger.append(self.path, {"task_id": "i", "role": "implementer",
                                  "ts_end": stamp()})
        ledger.append(self.path, {"task_id": "c", "kind": "check",
                                  "role": "reviewer", "ts_end": stamp()})
        self.assertEqual([r["task_id"] for r in
                          ledger.records(self.path, role="implementer")], ["i"])
        self.assertEqual([r["task_id"] for r in
                          ledger.records(self.path, kind="check")], ["c"])
        # kind defaults to "task" for records written before checks existed.
        self.assertEqual({r["task_id"] for r in
                          ledger.records(self.path, kind="task")}, {"r", "i"})

    def test_reads_into_the_rotated_generation_when_needed(self):
        with unittest.mock.patch.object(ledger, "MAX_BYTES", 1000):
            for i in range(12):
                ledger.append(self.path, {"task_id": f"t{i}", "ts_end": stamp(),
                                          "pad": "y" * 50})
        rows = ledger.records(self.path, limit=12)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]["task_id"], "t11")

    def test_limit_is_capped(self):
        self.write(count=3)
        self.assertLessEqual(len(ledger.records(self.path, limit=10**6)),
                             ledger.MAX_RECORDS)

    def test_a_record_whose_window_was_cut_in_half_is_not_misparsed(self):
        # Long lines force the tail reader to stitch blocks together.
        for i in range(30):
            ledger.append(self.path, {"task_id": f"t{i}", "ts_end": stamp(),
                                      "pad": "z" * 4000})
        rows = ledger.records(self.path, limit=30)
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["task_id"], "t29")


class Since(unittest.TestCase):
    def test_relative_units(self):
        for spec in ("7d", "24h", "30m"):
            self.assertIsNotNone(ledger.parse_since(spec))

    def test_iso_date_and_timestamp(self):
        self.assertEqual(ledger.parse_since("2026-10-28").year, 2026)
        self.assertIsNotNone(ledger.parse_since("2026-10-28T10:00:00Z"))

    def test_naive_timestamps_are_read_as_utc(self):
        self.assertEqual(
            ledger.parse_since("2026-10-28T10:00:00").tzinfo, timezone.utc)

    def test_none_means_no_bound(self):
        self.assertIsNone(ledger.parse_since(None))

    def test_nonsense_names_the_accepted_forms(self):
        with self.assertRaises(ValueError) as cm:
            ledger.parse_since("last tuesday")
        self.assertIn("7d", str(cm.exception))


class Summarize(unittest.TestCase):
    def rows(self):
        return [
            {"model": "opus", "role": "implementer", "rc": 0, "duration_ms": 1000,
             "input_tokens": 10, "output_tokens": 2, "cost_usd": 0.10,
             "ts_start": "2026-10-28T10:00:00+00:00"},
            {"model": "opus", "role": "implementer", "rc": 1, "duration_ms": 3000,
             "input_tokens": 20, "output_tokens": 4, "cost_usd": 0.20,
             "ts_start": "2026-10-28T11:00:00+00:00"},
            {"model": "haiku", "role": "researcher", "rc": 0, "duration_ms": 500,
             "input_tokens": 5, "output_tokens": 1, "cost_usd": 0.01,
             "cost_estimated": True, "ts_start": "2026-10-27T10:00:00+00:00"},
        ]

    def test_groups_by_model_with_counts_and_cost(self):
        summary = ledger.summarize(self.rows(), by="model")
        by = {g["model"]: g for g in summary["groups"]}
        self.assertEqual(by["opus"]["tasks"], 2)
        self.assertEqual(by["opus"]["failed"], 1)
        self.assertAlmostEqual(by["opus"]["cost_usd"], 0.30)
        self.assertEqual(by["haiku"]["tasks"], 1)

    def test_totals_and_the_estimated_cost_flag(self):
        summary = ledger.summarize(self.rows(), by="model")
        self.assertEqual(summary["totals"]["tasks"], 3)
        self.assertEqual(summary["totals"]["failed"], 1)
        self.assertAlmostEqual(summary["totals"]["cost_usd"], 0.31)
        self.assertTrue(summary["cost_estimated"])

    def test_percentiles_are_in_seconds(self):
        summary = ledger.summarize(self.rows(), by="model")
        by = {g["model"]: g for g in summary["groups"]}
        self.assertEqual(by["opus"]["p50_s"], 2.0)
        self.assertEqual(by["haiku"]["p50_s"], 0.5)
        self.assertEqual(by["haiku"]["p95_s"], 0.5)

    def test_group_by_day_and_role(self):
        days = ledger.summarize(self.rows(), by="day")
        self.assertEqual({g["day"] for g in days["groups"]},
                         {"2026-10-28", "2026-10-27"})
        roles = ledger.summarize(self.rows(), by="role")
        self.assertEqual({g["role"] for g in roles["groups"]},
                         {"implementer", "researcher"})

    def test_a_missing_group_value_is_unknown_not_a_crash(self):
        summary = ledger.summarize([{"rc": 0}], by="model")
        self.assertEqual(summary["groups"][0]["model"], "unknown")

    def test_empty_input(self):
        summary = ledger.summarize([], by="model")
        self.assertEqual(summary["groups"], [])
        self.assertEqual(summary["totals"]["tasks"], 0)
        self.assertFalse(summary["cost_estimated"])

    def test_an_invalid_grouping_names_the_valid_ones(self):
        with self.assertRaises(ValueError) as cm:
            ledger.summarize([], by="password")
        self.assertIn("model", str(cm.exception))

    def test_a_record_with_no_rc_is_not_counted_as_failed(self):
        summary = ledger.summarize([{"model": "opus"}], by="model")
        self.assertEqual(summary["totals"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
