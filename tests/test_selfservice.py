# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Self-service tools: a tenant changing its own credential, password and
committer identity — and never anyone else's."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge.service import config, gitconfig, htpasswd, ratelimit, selfservice
from agentbridge.service import secrets as secrets_mod
from agentbridge.service.tenant import Tenant, TenantError, credential_kind

API_KEY = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OAUTH = "sk-ant-oat01-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def run(coro):
    return asyncio.run(coro)


class CredentialKind(unittest.TestCase):
    def test_prefixes(self):
        self.assertEqual(credential_kind(API_KEY), "apikey")
        self.assertEqual(credential_kind(OAUTH), "oauth")

    def test_anything_else_is_refused(self):
        for token in ("", "sk-ant-xyz1", "ghp_token", "hunter2", "SK-ANT-API03"):
            with self.subTest(token=token), self.assertRaises(TenantError):
                credential_kind(token)

    def test_error_names_prefixes_but_not_the_value(self):
        with self.assertRaises(TenantError) as cm:
            credential_kind("some-private-value")
        message = str(cm.exception)
        self.assertIn("sk-ant-api", message)
        self.assertNotIn("some-private-value", message)
        self.assertNotIn(str(len("some-private-value")), message)


class SelfServiceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.patches = [
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "HTPASSWD_FILE", root / "htpasswd" / "users"),
        ]
        for p in self.patches:
            p.start()
        config.TENANTS_DIR.mkdir()
        self.tenant = Tenant(name="alice", vhost="ab-alice", bus_user="ab-alice",
                             bus_password="pw", max_workers=2)
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)
        for p in self.patches:
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)


class RegisterCredential(SelfServiceBase):
    def test_stores_under_the_prefix_s_kind(self):
        calls = []
        with mock.patch.object(secrets_mod, "create",
                               side_effect=lambda n, v: calls.append((n, v))):
            result = run(selfservice.register_credential(self.tenant, OAUTH))
        self.assertEqual(result["registered"], "oauth")
        self.assertEqual(calls, [("anthropic-alice-oauth", OAUTH)])

    def test_bad_token_stores_nothing(self):
        with mock.patch.object(secrets_mod, "create") as create, \
             self.assertRaises(TenantError):
            run(selfservice.register_credential(self.tenant, "nope"))
        create.assert_not_called()

    def test_podman_failure_becomes_a_caller_error(self):
        with mock.patch.object(secrets_mod, "create",
                               side_effect=secrets_mod.SecretError("store broken")), \
             self.assertRaises(TenantError) as cm:
            run(selfservice.register_credential(self.tenant, API_KEY))
        self.assertIn("store broken", str(cm.exception))

    def test_secret_name_is_the_caller_s_own(self):
        """A tenant must not be able to write another tenant's secret: the
        name is derived from the resolved tenant, never from an argument."""
        names = []
        with mock.patch.object(secrets_mod, "create",
                               side_effect=lambda n, v: names.append(n)):
            run(selfservice.register_credential(self.tenant, API_KEY))
        self.assertTrue(names[0].startswith("anthropic-alice-"))


class RevokeCredential(SelfServiceBase):
    def test_absent_secret_is_not_an_error(self):
        with mock.patch.object(secrets_mod, "remove", return_value=False):
            result = run(selfservice.revoke_credential(self.tenant, "oauth"))
        self.assertEqual(result, {"revoked": "oauth", "existed": False})

    def test_unknown_kind_refused_without_touching_the_store(self):
        with mock.patch.object(secrets_mod, "remove") as remove, \
             self.assertRaises(TenantError):
            run(selfservice.revoke_credential(self.tenant, "../../etc/passwd"))
        remove.assert_not_called()


class SetPassword(SelfServiceBase):
    def test_writes_own_entry_only(self):
        htpasswd.set_entry("bob", "bobs-long-password")
        run(selfservice.set_password(self.tenant, "alices-long-password"))
        self.assertEqual(htpasswd.entries(), ["alice", "bob"])

    def test_short_password_refused(self):
        with self.assertRaises(TenantError):
            run(selfservice.set_password(self.tenant, "short"))
        self.assertFalse(config.HTPASSWD_FILE.exists())


class GitIdentity(SelfServiceBase):
    def test_round_trips_through_whoami_s_reader(self):
        run(selfservice.set_git_identity(self.tenant, "Alice Doe", "alice@example.com"))
        self.assertEqual(gitconfig.read_identity(self.tenant),
                         {"name": "Alice Doe", "email": "alice@example.com"})

    def test_file_is_private_and_keeps_safe_directory(self):
        run(selfservice.set_git_identity(self.tenant, "Alice", "alice@example.com"))
        text = self.tenant.gitconfig.read_text()
        self.assertIn("[safe]", text)
        self.assertEqual(self.tenant.gitconfig.stat().st_mode & 0o777, 0o600)

    def test_injected_config_sections_are_refused(self):
        """A newline would let the caller append their own git config, and
        several git settings name a command git then runs."""
        attacks = [
            ("Alice\n[core]\n\tfsmonitor = /tmp/pwn", "alice@example.com"),
            ("Alice", "alice@example.com\n[include]\n\tpath = /etc/shadow"),
            ("Alice\r[core]", "alice@example.com"),
        ]
        for name, email in attacks:
            with self.subTest(name=name), self.assertRaises(TenantError):
                run(selfservice.set_git_identity(self.tenant, name, email))
        self.assertFalse(self.tenant.gitconfig.exists())

    def test_quotes_and_backslashes_are_escaped_not_refused(self):
        run(selfservice.set_git_identity(self.tenant, 'Al "Ace" O\\Brien',
                                         "alice@example.com"))
        text = self.tenant.gitconfig.read_text()
        self.assertIn(r'name = "Al \"Ace\" O\\Brien"', text)
        self.assertEqual(gitconfig.read_identity(self.tenant)["name"],
                         'Al "Ace" O\\Brien')

    def test_bad_emails_refused(self):
        for email in ("alice", "alice@example", "a b@example.com", "alice@@x.com",
                      "alice@example.com extra", "x" * 300 + "@example.com"):
            with self.subTest(email=email), self.assertRaises(TenantError):
                run(selfservice.set_git_identity(self.tenant, "Alice", email))

    def test_blank_name_refused(self):
        with self.assertRaises(TenantError):
            run(selfservice.set_git_identity(self.tenant, "   ", "alice@example.com"))

    def test_no_identity_reads_as_none(self):
        self.assertIsNone(gitconfig.read_identity(self.tenant))


class RateLimit(unittest.TestCase):
    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)

    def test_sixth_call_in_a_minute_is_refused(self):
        for _ in range(5):
            ratelimit.check("alice", "register_credential", now=1000.0)
        with self.assertRaises(ratelimit.RateLimited) as cm:
            ratelimit.check("alice", "register_credential", now=1000.0)
        self.assertIn("try again in", str(cm.exception))
        self.assertLessEqual(cm.exception.retry_after, 60)

    def test_window_slides(self):
        for i in range(5):
            ratelimit.check("alice", "set_password", now=1000.0 + i)
        ratelimit.check("alice", "set_password", now=1060.0)

    def test_refused_calls_are_not_counted(self):
        for _ in range(5):
            ratelimit.check("alice", "set_password", now=1000.0)
        for _ in range(3):
            with self.assertRaises(ratelimit.RateLimited):
                ratelimit.check("alice", "set_password", now=1030.0)
        # The five originals expire together, so the next minute is clean.
        ratelimit.check("alice", "set_password", now=1061.0)

    def test_limits_are_per_user_and_per_action(self):
        for _ in range(5):
            ratelimit.check("alice", "set_password", now=1000.0)
        ratelimit.check("bob", "set_password", now=1000.0)
        ratelimit.check("alice", "register_credential", now=1000.0)


if __name__ == "__main__":
    unittest.main()
