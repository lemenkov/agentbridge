# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Deterministic checks: the argv that runs them, and the verdict they leave.

The point of a check is that its answer can be trusted, so most of these
tests are about what the container does *not* get — a credential, a model, a
network, a writable workspace — and about the verdict being readable by
somebody other than the process that produced it.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge import ledger
from agentbridge.service import checkrunner, checks, config
from agentbridge.service.tenant import tenant_from_headers
from agentbridge.service.workspace import WorkspaceError

PASSWORD = "s3cr3t-bus-password"


class CheckBase(unittest.TestCase):
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
        (config.WORKSPACES_DIR / "alice" / "src").mkdir(parents=True)
        (config.TENANTS_DIR / "alice.json").write_text(json.dumps({
            "vhost": "ab-alice", "bus_user": "ab-alice",
            "bus_password": PASSWORD, "limits": {"max_workers": 2},
        }))
        self.alice = tenant_from_headers({config.USER_HEADER: "alice"})
        self.alice.checks_dir.mkdir(parents=True, exist_ok=True)


class Validation(unittest.TestCase):
    def test_command_must_be_a_non_empty_list(self):
        for bad in ([], "pytest -q", None, {}):
            with self.assertRaises(checks.CheckError):
                checks.validate_command(bad)

    def test_arguments_must_be_strings_without_nul(self):
        with self.assertRaises(checks.CheckError):
            checks.validate_command(["pytest", 7])
        with self.assertRaises(checks.CheckError):
            checks.validate_command(["pytest", "a\x00b"])

    def test_absurd_commands_are_refused(self):
        with self.assertRaises(checks.CheckError):
            checks.validate_command(["x"] * (checks.MAX_COMMAND_ARGS + 1))
        with self.assertRaises(checks.CheckError):
            checks.validate_command(["x" * (checks.MAX_ARG_LENGTH + 1)])

    def test_a_shell_invocation_is_allowed(self):
        # That shell runs inside the sandbox, so it grants the caller nothing
        # they did not already have.
        argv = ["sh", "-c", "pytest -q && ruff check ."]
        self.assertEqual(checks.validate_command(argv), argv)

    def test_timeout_bounds(self):
        self.assertEqual(checks.validate_timeout(300), 300)
        for bad in (0, -1, checks.MAX_TIMEOUT + 1):
            with self.assertRaises(checks.CheckError):
                checks.validate_timeout(bad)

    def test_check_ids_are_validated(self):
        self.assertTrue(checks.validate_check_id(checks.new_check_id()))
        for bad in ("", "../etc/passwd", "ZZZ", "abc", None):
            with self.assertRaises(checks.CheckError):
                checks.validate_check_id(bad)


class BuildCommand(CheckBase):
    def build(self, command=None, role="reviewer", workdir=".", timeout=300):
        return checks.build_command(
            self.alice, role, "abcdef123456", command or ["pytest", "-q"],
            workdir, timeout)

    def test_no_credential_no_claude_no_model_env(self):
        argv = " ".join(self.build())
        self.assertNotIn("--secret", argv)
        self.assertNotIn("claude", argv)
        self.assertNotIn("ANTHROPIC", argv)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", argv)

    def test_no_network(self):
        self.assertIn("--network=none", self.build())
        self.assertNotIn("--network=host", self.build())

    def test_the_workspace_is_an_overlay_so_writes_are_discarded(self):
        argv = self.build()
        self.assertIn(f"{self.alice.workspace}:/workspace:O", argv)
        self.assertNotIn(f"{self.alice.workspace}:/workspace:rw", argv)

    def test_no_bus_credential_appears(self):
        argv = " ".join(self.build())
        self.assertNotIn(PASSWORD, argv)
        self.assertNotIn("amqp://", argv)

    def test_the_caller_never_names_an_image(self):
        argv = self.build(role="reviewer")
        self.assertIn("agentbridge-pydev", argv)      # the reviewer role's image
        argv = self.build(role="researcher")
        self.assertIn("agentbridge-base", argv)

    def test_the_command_is_passed_as_argv_not_a_shell_string(self):
        argv = self.build(["sh", "-c", "echo hi; rm -rf /"])
        # The dangerous string is one argv element, never concatenated into a
        # shell invocation on the host.
        self.assertEqual(argv[-3:], ["sh", "-c", "echo hi; rm -rf /"])
        self.assertNotIn("sh -c", " ".join(argv[:-3]))

    def test_the_runner_owns_the_result_and_log_paths(self):
        argv = self.build()
        self.assertIn("agentbridge.service.checkrunner", argv)
        log, result = checks._paths(self.alice, "abcdef123456")
        self.assertIn(str(result), argv)
        self.assertIn(str(log), argv)
        # Outside the workspace: a check's subject must not be able to edit
        # its verdict.
        self.assertNotIn(str(self.alice.workspace), str(result))
        self.assertNotIn(str(self.alice.workspace), str(log))

    def test_the_unit_carries_the_caps_and_the_deadline(self):
        argv = self.build(timeout=42)
        self.assertIn("--property=RuntimeMaxSec=42", argv)
        self.assertIn(f"--property=MemoryMax={config.WORKER_MEMORY_MAX}", argv)
        self.assertIn("--unit=agentbridge-alice-check-abcdef123456.service", argv)

    def test_workdir_is_resolved_inside_the_workspace(self):
        argv = self.build(workdir="src")
        self.assertIn("/workspace/src", argv)
        for bad in ("../../etc", "/etc"):
            with self.assertRaises(WorkspaceError):
                self.build(workdir=bad)

    def test_a_missing_workdir_is_a_caller_error(self):
        with self.assertRaises(checks.CheckError):
            self.build(workdir="nope")

    def test_the_ledger_is_passed_so_the_unit_records_itself(self):
        argv = self.build()
        self.assertIn(str(self.alice.ledger), argv)
        self.assertIn("--ledger", argv)


class Runner(unittest.TestCase):
    """The process the unit runs, exercised for real."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.result = self.dir / "r.json"
        self.log = self.dir / "r.log"

    def run_it(self, *command, ledger_path=None):
        argv = ["--result", str(self.result), "--log", str(self.log),
                "--check-id", "abcdef123456"]
        if ledger_path:
            argv += ["--ledger", str(ledger_path), "--role", "reviewer"]
        argv += ["--", *command]
        return checkrunner.main(argv)

    def read(self):
        return json.loads(self.result.read_text())

    def test_a_passing_command(self):
        self.run_it(sys.executable, "-c", "print('all good')")
        row = self.read()
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["rc"], 0)
        self.assertIn("all good", self.log.read_text())
        self.assertIsInstance(row["duration_s"], float)

    def test_a_failing_command_records_its_code(self):
        self.run_it(sys.executable, "-c", "import sys; sys.exit(3)")
        self.assertEqual(self.read()["rc"], 3)

    def test_stderr_is_captured_too(self):
        self.run_it(sys.executable, "-c",
                    "import sys; print('out'); print('err', file=sys.stderr)")
        text = self.log.read_text()
        self.assertIn("out", text)
        self.assertIn("err", text)

    def test_a_command_that_cannot_run_is_a_check_failure(self):
        self.run_it("/nonexistent/binary")
        row = self.read()
        self.assertEqual(row["rc"], 127)
        self.assertIn("error", row)
        self.assertEqual(row["state"], "done")

    def test_running_is_announced_before_the_command(self):
        # A check killed by its deadline leaves the "running" record, which is
        # how a timeout is told apart from a check that never started.
        seen = {}
        real = checkrunner._write_atomic

        def spy(path, payload):
            seen.setdefault("first", payload)
            return real(path, payload)

        with mock.patch.object(checkrunner, "_write_atomic", spy):
            self.run_it(sys.executable, "-c", "pass")
        self.assertEqual(seen["first"]["state"], "running")
        self.assertIsNone(seen["first"]["rc"])

    def test_the_result_is_never_half_written(self):
        self.run_it(sys.executable, "-c", "pass")
        # One atomic replace, so no temp file survives.
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["r.json", "r.log"])

    def test_it_appends_a_check_line_to_the_ledger(self):
        path = self.dir / "alice.ledger.jsonl"
        self.run_it(sys.executable, "-c", "import sys; sys.exit(1)",
                    ledger_path=path)
        rows = ledger.records(path, limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "check")
        self.assertEqual(rows[0]["rc"], 1)
        self.assertEqual(rows[0]["role"], "reviewer")
        self.assertEqual(rows[0]["task_id"], "abcdef123456")

    def test_a_broken_ledger_does_not_change_the_verdict(self):
        self.run_it(sys.executable, "-c", "pass",
                    ledger_path=self.dir / "no-such-dir" / "l.jsonl")
        self.assertEqual(self.read()["rc"], 0)
        self.assertIn("ledger append failed", self.log.read_text())

    def test_no_command_is_an_error(self):
        with self.assertRaises(SystemExit):
            checkrunner.main(["--result", str(self.result), "--log", str(self.log)])


class Status(CheckBase):
    def write_result(self, check_id, payload):
        _, path = checks._paths(self.alice, check_id)
        path.write_text(json.dumps(payload))

    def write_log(self, check_id, text):
        log, _ = checks._paths(self.alice, check_id)
        log.write_text(text)

    def status(self, check_id, unit_present):
        return asyncio.run(checks.status(self.alice, check_id, unit_present))

    def test_a_finished_check_reports_its_code(self):
        self.write_result("aaaaaaaaaaaa", {
            "state": "done", "rc": 0, "duration_s": 1.5, "started": "t"})
        self.write_log("aaaaaaaaaaaa", "1 passed")
        row = self.status("aaaaaaaaaaaa", unit_present=False)
        self.assertEqual((row["state"], row["rc"]), ("done", 0))
        self.assertEqual(row["tail"], "1 passed")
        self.assertEqual(row["log_bytes"], 8)

    def test_a_running_check_with_a_live_unit(self):
        self.write_result("bbbbbbbbbbbb", {"state": "running", "rc": None})
        self.assertEqual(self.status("bbbbbbbbbbbb", True)["state"], "running")

    def test_a_running_record_with_no_unit_is_a_timeout(self):
        # Not "running forever": the same reasoning as the fleet view, where
        # systemd decides whether something still exists.
        self.write_result("cccccccccccc", {"state": "running", "rc": None})
        row = self.status("cccccccccccc", False)
        self.assertEqual(row["state"], "timeout")
        self.assertIn("timeout", row["note"])

    def test_a_timeout_is_finalized_and_recorded_in_the_ledger(self):
        # The unit was killed, so it could not write its own verdict; the
        # first reader to notice does it, exactly once.
        self.write_result("222222222222", {
            "state": "running", "rc": None, "role": "reviewer",
            "started": "2026-10-28T12:00:00+00:00"})
        row = self.status("222222222222", unit_present=False)
        self.assertEqual(row["state"], "timeout")
        self.assertEqual(row["rc"], checks.TIMEOUT_RC)

        entries = ledger.records(self.alice.ledger, limit=5)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "check")
        self.assertEqual(entries[0]["rc"], checks.TIMEOUT_RC)
        self.assertEqual(entries[0]["role"], "reviewer")
        self.assertTrue(entries[0]["timed_out"])

        # Reading again must not append a second row: the rewritten result
        # file is what makes it idempotent.
        again = self.status("222222222222", unit_present=False)
        self.assertEqual(again["state"], "timeout")
        self.assertEqual(len(ledger.records(self.alice.ledger, limit=5)), 1)

    def test_a_timed_out_check_counts_as_failed(self):
        self.write_result("333333333333", {
            "state": "running", "rc": None, "role": "reviewer",
            "started": "2026-10-28T12:00:00+00:00"})
        self.status("333333333333", unit_present=False)
        summary = ledger.summarize(
            ledger.records(self.alice.ledger, limit=5), by="kind")
        self.assertEqual(summary["totals"]["failed"], 1)

    def test_an_unknown_check_is_a_caller_error(self):
        with self.assertRaises(checks.CheckError):
            self.status("dddddddddddd", False)

    def test_the_tail_is_the_end_of_a_long_log(self):
        self.write_result("eeeeeeeeeeee", {"state": "done", "rc": 0})
        self.write_log("eeeeeeeeeeee", "x" * 20000 + "THE-END")
        row = self.status("eeeeeeeeeeee", False)
        self.assertTrue(row["tail"].endswith("THE-END"))
        self.assertEqual(len(row["tail"]), checks.TAIL_BYTES)
        self.assertTrue(row["truncated"])
        self.assertEqual(row["log_bytes"], 20007)

    def test_wait_for_returns_as_soon_as_it_is_done(self):
        self.write_result("ffffffffffff", {"state": "done", "rc": 0})

        async def present():
            return False

        row = asyncio.run(checks.wait_for(
            self.alice, "ffffffffffff", 5, present, poll=0.01))
        self.assertEqual(row["state"], "done")

    def test_wait_for_gives_up_and_reports_running(self):
        self.write_result("111111111111", {"state": "running", "rc": None})

        async def present():
            return True

        row = asyncio.run(checks.wait_for(
            self.alice, "111111111111", 0.05, present, poll=0.01))
        self.assertEqual(row["state"], "running")


class UnitNaming(CheckBase):
    def test_a_check_is_distinguishable_from_a_worker(self):
        self.assertTrue(checks.is_check("check-abcdef123456"))
        self.assertFalse(checks.is_check("researcher-1"))

    def test_the_unit_shares_the_tenant_prefix(self):
        name = checks.unit_name(self.alice, "abcdef123456")
        self.assertTrue(name.startswith(self.alice.unit_prefix))
        self.assertEqual(name, "agentbridge-alice-check-abcdef123456.service")


if __name__ == "__main__":
    unittest.main()
