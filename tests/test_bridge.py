# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import os
import tempfile
import unittest

from agentbridge.bridge import resolve_amqp_url


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


if __name__ == "__main__":
    unittest.main()
