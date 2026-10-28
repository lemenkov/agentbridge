# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The driver seam: what reaches the bus, and what must never reach it.

The fixture is trimmed from a real capture of claude 2.1.309 (plan M-6); the
init event's full tool and skill lists are dropped, since they describe the
capturing host rather than the grammar.
"""

import json
import unittest
from pathlib import Path

from agentbridge.drivers import (
    DEFAULT_DRIVER,
    StreamJsonDriver,
    TextDriver,
    make_driver,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stream-json.jsonl"


def run(driver, lines):
    """Feed lines, then finish. Returns (events, metrics, driver_error)."""
    events = []
    for line in lines:
        events += driver.feed(line)
    metrics, error = driver.finish()
    return events, metrics, error


class Selection(unittest.TestCase):
    def test_default_is_text_so_any_command_still_works(self):
        self.assertEqual(DEFAULT_DRIVER, "claude-text")
        self.assertIsInstance(make_driver(DEFAULT_DRIVER), TextDriver)

    def test_unknown_driver_names_the_alternatives(self):
        with self.assertRaises(ValueError) as cm:
            make_driver("acp")
        self.assertIn("claude-stream-json", str(cm.exception))

    def test_each_task_gets_a_fresh_driver(self):
        self.assertIsNot(make_driver("claude-stream-json"),
                         make_driver("claude-stream-json"))


class Text(unittest.TestCase):
    def test_every_line_is_output_and_blanks_are_dropped(self):
        events, metrics, error = run(TextDriver(), ["one\n", "\n", "  \n", "two\n"])
        self.assertEqual([e.body for e in events], ["one", "two"])
        self.assertTrue(all(e.kind == "output" for e in events))
        self.assertIsNone(metrics)
        self.assertIsNone(error)

    def test_adds_no_flags_to_the_wrapped_command(self):
        self.assertEqual(TextDriver().extra_args, ())


class StreamJson(unittest.TestCase):
    def setUp(self):
        self.lines = FIXTURE.read_text().splitlines(keepends=True)
        self.events, self.metrics, self.error = run(StreamJsonDriver(), self.lines)

    def test_requests_machine_readable_output(self):
        self.assertEqual(
            StreamJsonDriver().extra_args,
            ("--output-format", "stream-json", "--verbose"),
        )

    def test_the_answer_is_published_once_from_the_result_event(self):
        outputs = [e.body for e in self.events if e.kind == "output"]
        self.assertEqual(outputs, ["The file contains: hello from the fixture"])

    def test_tool_calls_become_progress_naming_only_the_tool(self):
        tools = [e for e in self.events if e.kind == "progress" and e.tool]
        self.assertEqual([e.tool for e in tools], ["Read"])
        # The tool's input is in the fixture and must not travel with it.
        self.assertNotIn("note.txt", json.dumps([vars(e) for e in self.events]))

    def test_a_turn_without_a_tool_call_still_reports_liveness(self):
        phases = [e.phase for e in self.events if e.kind == "progress" and e.phase]
        self.assertEqual(phases, ["assistant", "assistant"])

    def test_thinking_and_intermediate_text_never_leave_the_driver(self):
        blob = json.dumps([vars(e) for e in self.events])
        self.assertNotIn("SECRET-THINKING-MUST-NOT-LEAK", blob)
        self.assertNotIn("INTERMEDIATE-TEXT", blob)

    def test_metrics_come_from_the_result_event(self):
        self.assertIsNone(self.error)
        self.assertEqual(self.metrics["model"], "claude-haiku-5-5")
        self.assertEqual(self.metrics["num_turns"], 3)
        self.assertEqual(self.metrics["agent_duration_ms"], 2567)
        self.assertEqual(self.metrics["input_tokens"], 5)
        self.assertEqual(self.metrics["output_tokens"], 13)
        self.assertEqual(self.metrics["cache_read_tokens"], 11334)
        self.assertEqual(self.metrics["cache_creation_tokens"], 14730)
        self.assertAlmostEqual(self.metrics["cost_usd"], 0.00306154)
        self.assertFalse(self.metrics["is_error"])

    def test_a_subscription_credential_marks_the_cost_as_notional(self):
        # The fixture's apiKeySource is "none", i.e. an OAuth/subscription run.
        self.assertTrue(self.metrics["cost_estimated"])

    def test_an_api_key_run_reports_cost_as_billed(self):
        lines = list(self.lines)
        lines[0] = json.dumps({"type": "system", "subtype": "init",
                               "model": "claude-opus-5-5",
                               "apiKeySource": "ANTHROPIC_API_KEY"}) + "\n"
        _, metrics, _ = run(StreamJsonDriver(), lines)
        self.assertFalse(metrics["cost_estimated"])

    def test_several_models_are_listed_rather_than_guessed(self):
        lines = list(self.lines)
        last = json.loads(lines[-1])
        last["modelUsage"] = {"claude-opus-5-5": {}, "claude-haiku-5-5": {}}
        lines[-1] = json.dumps(last) + "\n"
        _, metrics, _ = run(StreamJsonDriver(), lines)
        self.assertEqual(metrics["models"],
                         ["claude-haiku-5-5", "claude-opus-5-5"])
        # The init hint is not passed off as the answer.
        self.assertEqual(metrics["model"], "claude-haiku-5-5")


class StreamJsonAnomalies(unittest.TestCase):
    def test_a_non_json_line_is_forwarded_and_counted(self):
        lines = FIXTURE.read_text().splitlines(keepends=True)
        lines.insert(2, "podman: WARN something happened\n")
        events, metrics, error = run(StreamJsonDriver(), lines)
        bodies = [e.body for e in events if e.kind == "output"]
        self.assertIn("podman: WARN something happened", bodies)
        self.assertEqual(metrics["unparsed_lines"], 1)
        self.assertIsNone(error)

    def test_json_that_is_not_an_object_is_treated_the_same(self):
        _, metrics, error = run(StreamJsonDriver(),
                                ['"a bare string"\n', "[1,2]\n"])
        self.assertIsNone(metrics)
        self.assertIn("without a result event", error)

    def test_no_result_event_means_no_metrics_and_an_error(self):
        lines = FIXTURE.read_text().splitlines(keepends=True)[:-1]
        events, metrics, error = run(StreamJsonDriver(), lines)
        self.assertIsNone(metrics)
        self.assertIn("without a result event", error)
        # Progress still happened, so the failure is diagnosable.
        self.assertTrue(any(e.kind == "progress" for e in events))

    def test_an_error_result_still_yields_metrics(self):
        lines = FIXTURE.read_text().splitlines(keepends=True)
        last = json.loads(lines[-1])
        last.update(subtype="error_during_execution", is_error=True, result="")
        lines[-1] = json.dumps(last) + "\n"
        events, metrics, error = run(StreamJsonDriver(), lines)
        self.assertIsNone(error)
        self.assertTrue(metrics["is_error"])
        self.assertEqual(metrics["subtype"], "error_during_execution")
        # An empty result is not published as an empty output message.
        self.assertEqual([e for e in events if e.kind == "output"], [])

    def test_a_truncated_line_does_not_raise(self):
        _, metrics, error = run(StreamJsonDriver(), ['{"type":"result"'])
        self.assertIsNone(metrics)
        self.assertIsNotNone(error)

    def test_missing_usage_fields_are_omitted_not_zeroed(self):
        _, metrics, _ = run(StreamJsonDriver(),
                            ['{"type":"result","subtype":"success","result":"hi"}\n'])
        self.assertNotIn("input_tokens", metrics)
        self.assertNotIn("cost_usd", metrics)


if __name__ == "__main__":
    unittest.main()
