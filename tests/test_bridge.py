# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import os
import tempfile
import unittest

from agentbridge.bridge import option_args, resolve_amqp_url, validate_task_options


class ResolveAmqpUrl(unittest.TestCase):
    def test_flag_wins(self):
        env = {"AMQP_URL": "amqp://env/"}
        self.assertEqual(resolve_amqp_url("amqp://flag/", None, env), "amqp://flag/")

    def test_file_beats_env(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("amqp://file/\nignored\n")
        try:
            env = {"AMQP_URL": "amqp://env/"}
            self.assertEqual(resolve_amqp_url(None, fh.name, env), "amqp://file/")
        finally:
            os.unlink(fh.name)

    def test_empty_file_rejected(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            pass
        try:
            with self.assertRaises(ValueError):
                resolve_amqp_url(None, fh.name, {"AMQP_URL": "amqp://env/"})
        finally:
            os.unlink(fh.name)

    def test_env_fallback(self):
        self.assertEqual(resolve_amqp_url(None, None, {"AMQP_URL": "amqp://env/"}),
                         "amqp://env/")

    def test_no_default(self):
        with self.assertRaises(ValueError):
            resolve_amqp_url(None, None, {})
        with self.assertRaises(ValueError):
            resolve_amqp_url(None, None, {"AMQP_URL": "  "})


class TaskOptions(unittest.TestCase):
    def test_valid(self):
        for model in ("fable", "opus", "sonnet", "haiku", "claude-opus-5-5",
                      "claude-haiku-5-5"):
            self.assertEqual(validate_task_options(model, None), {"model": model})
        self.assertEqual(validate_task_options(None, "xhigh"), {"effort": "xhigh"})
        self.assertEqual(validate_task_options(None, None), {})

    def test_injection_rejected(self):
        for model in ("--dangerously-skip-permissions", "opus --effort max",
                      "gpt-4", "Opus", "", "claude-", 42):
            with self.assertRaises(ValueError):
                validate_task_options(model, None)
        for effort in ("extreme", "--max", "", 3):
            with self.assertRaises(ValueError):
                validate_task_options(None, effort)

    def test_args_order(self):
        self.assertEqual(option_args({"effort": "high", "model": "opus"}),
                         ["--model", "opus", "--effort", "high"])
        self.assertEqual(option_args({}), [])


if __name__ == "__main__":
    unittest.main()


class ModelAllowlist(unittest.TestCase):
    """A backend whose model names are not Anthropic's still needs a closed set.

    `qwen3-coder` and `gpt-oss:20b` match no plausible MODEL_RE, and widening
    the pattern until they fit would widen it until anything fits. So the
    pattern is replaced by an explicit list, which comes from the operator's
    backends.toml and never from a task.
    """

    ALLOW = ("qwen3-coder", "gpt-oss:20b")

    def test_an_allowlisted_name_is_accepted(self):
        for model in self.ALLOW:
            self.assertEqual(validate_task_options(model, None, self.ALLOW),
                             {"model": model})

    def test_a_claude_name_is_refused_when_an_allowlist_is_given(self):
        # The worker is pinned to one backend; a Claude alias would be sent to
        # an endpoint that has never heard of it.
        for model in ("opus", "claude-opus-5-5"):
            with self.assertRaises(ValueError):
                validate_task_options(model, None, self.ALLOW)

    def test_the_allowlist_is_exact_not_a_prefix_or_substring(self):
        for model in ("qwen3", "qwen3-coder:latest", "QWEN3-CODER",
                      "qwen3-coder --effort max", "gpt-oss"):
            with self.assertRaises(ValueError):
                validate_task_options(model, None, self.ALLOW)

    def test_injection_is_still_refused_under_an_allowlist(self):
        for model in ("--dangerously-skip-permissions", "", 42, None.__class__):
            with self.assertRaises(ValueError):
                validate_task_options(model, None, self.ALLOW)

    def test_an_empty_allowlist_refuses_everything(self):
        # It must not fall through to MODEL_RE: a backend that declares no
        # models can run none. (No --model-allow at all passes None, which is
        # the Claude-names default.)
        with self.assertRaises(ValueError):
            validate_task_options("opus", None, [])

    def test_no_allowlist_keeps_the_claude_pattern(self):
        self.assertEqual(validate_task_options("opus", None, None),
                         {"model": "opus"})
        with self.assertRaises(ValueError):
            validate_task_options("qwen3-coder", None, None)

    def test_the_error_names_what_is_allowed(self):
        with self.assertRaises(ValueError) as caught:
            validate_task_options("opus", None, self.ALLOW)
        for name in self.ALLOW:
            self.assertIn(name, str(caught.exception))

    def test_effort_validation_is_unaffected(self):
        self.assertEqual(
            validate_task_options("qwen3-coder", "high", self.ALLOW),
            {"model": "qwen3-coder", "effort": "high"})
        with self.assertRaises(ValueError):
            validate_task_options("qwen3-coder", "extreme", self.ALLOW)
