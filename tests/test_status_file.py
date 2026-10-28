# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Worker status files: transitions, atomicity, and staying out of the way."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from agentbridge import status


class Transitions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "alice.status"
        self.path = self.dir / "researcher-1.json"
        self.writer = status.StatusWriter(
            self.path, agent="researcher-1", role="researcher",
            options={"model": "haiku"}, driver="claude-stream-json")

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def test_the_expected_sequence(self):
        self.writer.set("starting")
        self.assertEqual(self.read()["state"], "starting")
        self.writer.set("idle")
        self.assertEqual(self.read()["state"], "idle")

        self.writer.task_started("T1", {"model": "opus", "effort": "high"})
        row = self.read()
        self.assertEqual(row["state"], "working")
        self.assertEqual(row["task_id"], "T1")
        self.assertEqual((row["model"], row["effort"]), ("opus", "high"))
        self.assertIn("task_started", row)

        self.writer.progress("Bash", None)
        self.assertEqual(self.read()["last_tool"], "Bash")

        self.writer.task_finished(0)
        row = self.read()
        self.assertEqual(row["state"], "idle")
        self.assertEqual((row["tasks_done"], row["last_rc"]), (1, 0))
        # A finished task is no longer the current one.
        self.assertNotIn("task_id", row)

        self.writer.retiring()
        self.assertEqual(self.read()["state"], "retiring")

    def test_states_are_the_documented_set(self):
        for state in status.STATES:
            self.writer.set(state)
            self.assertEqual(self.read()["state"], state)

    def test_tasks_done_accumulates_and_keeps_the_last_rc(self):
        for rc in (0, 0, 2):
            self.writer.task_started("T", {})
            self.writer.task_finished(rc)
        row = self.read()
        self.assertEqual(row["tasks_done"], 3)
        self.assertEqual(row["last_rc"], 2)

    def test_a_new_task_clears_the_previous_tool(self):
        self.writer.task_started("T1", {})
        self.writer.progress("Bash", None)
        self.writer.task_finished(0)
        self.writer.task_started("T2", {})
        self.assertNotIn("last_tool", self.read())

    def test_the_file_is_private_and_the_directory_is_created(self):
        self.writer.set("idle")
        self.assertTrue(self.dir.is_dir())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_path_means_no_file_and_no_error(self):
        writer = status.StatusWriter(None, agent="x")
        writer.set("idle")
        writer.task_started("T", {})
        writer.task_finished(0)
        writer.remove()                      # all no-ops

    def test_remove_deletes_the_file_and_tolerates_a_second_call(self):
        self.writer.set("idle")
        self.writer.remove()
        self.assertFalse(self.path.exists())
        self.writer.remove()

    def test_an_unwritable_directory_does_not_raise(self):
        writer = status.StatusWriter(
            Path(self._tmp.name) / "nope" / "deep" / "x.json", agent="x")
        # Parent creation succeeds here; make it genuinely impossible instead.
        writer.path = Path("/proc/definitely-not-writable/x.json")
        writer.set("idle")               # must not raise

    def test_a_tool_change_always_writes_but_repeats_are_throttled(self):
        self.writer.set("idle")
        self.writer.progress("Read", None)
        # A change of tool is the page's most useful signal: never suppressed.
        self.assertEqual(self.read()["last_tool"], "Read")
        written = self.path.stat().st_mtime_ns
        self.writer.progress("Read", None)       # same tool, within interval
        self.assertEqual(self.path.stat().st_mtime_ns, written)
        self.writer.progress("Bash", None)       # a change: writes
        self.assertEqual(self.read()["last_tool"], "Bash")

    def test_a_bare_assistant_turn_is_throttled(self):
        self.writer.set("idle")
        written = self.path.stat().st_mtime_ns
        self.writer.progress(None, "assistant")
        self.assertEqual(self.path.stat().st_mtime_ns, written)


class Atomicity(unittest.TestCase):
    def test_a_reader_never_sees_a_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            writer = status.StatusWriter(path, agent="w", role="r")
            writer.set("idle")
            stop = threading.Event()
            errors = []

            def reader():
                while not stop.is_set():
                    try:
                        text = path.read_text()
                        if text:
                            json.loads(text)
                    except FileNotFoundError:
                        pass
                    except json.JSONDecodeError as exc:
                        errors.append(exc)
                        return

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                for i in range(300):
                    writer.set("working", task_id=f"T{i}", last_tool="Bash" * 20)
            finally:
                stop.set()
                thread.join()
            self.assertEqual(errors, [])

    def test_no_temp_files_are_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            writer = status.StatusWriter(path, agent="w")
            for i in range(20):
                writer.set("working", task_id=str(i))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["w.json"])


class ReadDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_reads_every_worker(self):
        for name in ("a-1", "b-2"):
            status.StatusWriter(self.dir / f"{name}.json", agent=name).set("idle")
        self.assertEqual(sorted(r["agent"] for r in status.read_dir(self.dir)),
                         ["a-1", "b-2"])

    def test_a_missing_directory_is_empty(self):
        self.assertEqual(status.read_dir(self.dir / "nope"), [])

    def test_junk_is_skipped_rather_than_fatal(self):
        status.StatusWriter(self.dir / "good.json", agent="good").set("idle")
        (self.dir / "bad.json").write_text("{not json")
        (self.dir / "notes.txt").write_text("ignored")
        (self.dir / ".hidden.json").write_text("{}")
        rows = status.read_dir(self.dir)
        self.assertEqual([r["agent"] for r in rows], ["good"])


if __name__ == "__main__":
    unittest.main()
