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
