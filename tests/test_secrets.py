# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Podman secrets: the value goes on stdin, never into argv or an error."""

import subprocess
import unittest
from unittest import mock

from agentbridge.service import secrets as secrets_mod

TOKEN = "sk-ant-oat01-super-secret-value"


class FakeRunner:
    """Records calls and replies per-argv, like a very small podman."""

    def __init__(self, rc=0, stdout=b"", stderr=b""):
        self.calls = []
        self.rc, self.stdout, self.stderr = rc, stdout, stderr

    def __call__(self, argv, stdin=None):
        self.calls.append((list(argv), stdin))
        return subprocess.CompletedProcess(list(argv), self.rc,
                                           self.stdout, self.stderr)

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]


class WithReplace(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(secrets_mod, "_supports_replace",
                                  lambda: True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_create_uses_replace_and_stdin(self):
        runner = FakeRunner()
        secrets_mod.create("anthropic-alice-oauth", TOKEN, runner=runner)
        self.assertEqual(runner.argvs, [[
            "podman", "secret", "create", "--replace",
            "anthropic-alice-oauth", "-",
        ]])
        self.assertEqual(runner.calls[0][1], TOKEN.encode())

    def test_value_never_appears_in_argv(self):
        runner = FakeRunner()
        secrets_mod.create("anthropic-alice-oauth", TOKEN, runner=runner)
        self.assertNotIn(TOKEN, " ".join(runner.argvs[0]))

    def test_empty_value_refused_without_calling_podman(self):
        runner = FakeRunner()
        with self.assertRaises(secrets_mod.SecretError):
            secrets_mod.create("anthropic-alice-oauth", "", runner=runner)
        self.assertEqual(runner.argvs, [])

    def test_failure_message_excludes_the_value(self):
        runner = FakeRunner(rc=125, stderr=b"Error: store is broken")
        with self.assertRaises(secrets_mod.SecretError) as cm:
            secrets_mod.create("anthropic-alice-oauth", TOKEN, runner=runner)
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertIn("store is broken", str(cm.exception))


class WithoutReplace(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(secrets_mod, "_supports_replace",
                                  lambda: False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_create_falls_back_to_rm_then_create(self):
        runner = FakeRunner()
        secrets_mod.create("anthropic-alice-oauth", TOKEN, runner=runner)
        self.assertEqual(runner.argvs, [
            ["podman", "secret", "rm", "anthropic-alice-oauth"],
            ["podman", "secret", "create", "anthropic-alice-oauth", "-"],
        ])
        self.assertEqual(runner.calls[-1][1], TOKEN.encode())

    def test_absent_secret_does_not_block_the_fallback(self):
        class Runner(FakeRunner):
            def __call__(self, argv, stdin=None):
                self.calls.append((list(argv), stdin))
                if argv[2] == "rm":
                    return subprocess.CompletedProcess(
                        list(argv), 1, b"", b"Error: no such secret: x")
                return subprocess.CompletedProcess(list(argv), 0, b"", b"")

        runner = Runner()
        secrets_mod.create("x", TOKEN, runner=runner)
        self.assertEqual(len(runner.argvs), 2)


class Removal(unittest.TestCase):
    def test_absent_secret_is_success(self):
        runner = FakeRunner(rc=1, stderr=b"Error: no such secret: nope")
        self.assertFalse(secrets_mod.remove("nope", runner=runner))

    def test_other_failures_raise(self):
        runner = FakeRunner(rc=125, stderr=b"Error: permission denied")
        with self.assertRaises(secrets_mod.SecretError):
            secrets_mod.remove("x", runner=runner)

    def test_names_parses_the_list(self):
        runner = FakeRunner(stdout=b"anthropic-alice-oauth\nanthropic-bob-apikey\n")
        self.assertEqual(secrets_mod.names(runner=runner),
                         {"anthropic-alice-oauth", "anthropic-bob-apikey"})


if __name__ == "__main__":
    unittest.main()
