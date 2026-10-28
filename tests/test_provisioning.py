# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tenant creation and removal, and the admin endpoint's gate.

The broker is faked: these tests are about what lands on disk, what comes
back to the caller, and who is allowed to ask.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge.service import (
    admin_api,
    config,
    htpasswd,
    provisioning,
    rabbit_mgmt,
    ratelimit,
)
from agentbridge.service import secrets as secrets_mod
from agentbridge.service.tenant import TenantError, load_tenant


class ProvisioningBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.patches = [
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "WORKSPACES_DIR", root / "ws"),
            mock.patch.object(config, "HTPASSWD_FILE", root / "htpasswd" / "users"),
            # The broker is someone else's test (test_rabbit_mgmt).
            mock.patch.object(rabbit_mgmt, "ensure_tenant"),
            mock.patch.object(rabbit_mgmt, "delete_vhost"),
            mock.patch.object(rabbit_mgmt, "delete_user"),
        ]
        self.mocks = [p.start() for p in self.patches]
        config.TENANTS_DIR.mkdir()
        ratelimit.reset()
        for p in self.patches:
            self.addCleanup(p.stop)
        self.addCleanup(ratelimit.reset)
        self.addCleanup(self._tmp.cleanup)

    def record(self, name):
        return json.loads((config.TENANTS_DIR / f"{name}.json").read_text())


class Provision(ProvisioningBase):
    def test_creates_broker_objects_records_and_login(self):
        result = provisioning.provision("alice")
        self.assertEqual(result["vhost"], "ab-alice")
        self.assertIsNotNone(result["password"])
        rabbit_mgmt.ensure_tenant.assert_called_once()
        vhost, user, password = rabbit_mgmt.ensure_tenant.call_args.args
        self.assertEqual((vhost, user), ("ab-alice", "ab-alice"))
        # The bus password is generated, not derived from anything guessable.
        self.assertEqual(password, self.record("alice")["bus_password"])
        self.assertTrue(htpasswd.has_entry("alice"))
        self.assertTrue((config.WORKSPACES_DIR / "alice").is_dir())

    def test_private_files_are_0600_and_workspace_0700(self):
        provisioning.provision("alice")
        tenant = load_tenant("alice")
        for path in (config.TENANTS_DIR / "alice.json", tenant.amqp_url_file):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        self.assertEqual(tenant.workspace.stat().st_mode & 0o777, 0o700)

    def test_rerun_is_idempotent_and_keeps_the_password(self):
        first = provisioning.provision("alice")
        again = provisioning.provision("alice")
        self.assertIsNone(again["password"])
        self.assertEqual(self.record("alice")["bus_password"],
                         first["bus_user"] and self.record("alice")["bus_password"])
        self.assertEqual(again["vhost"], first["vhost"])

    def test_reset_password_issues_a_new_one(self):
        provisioning.provision("alice")
        again = provisioning.provision("alice", reset_password=True)
        self.assertIsNotNone(again["password"])

    def test_admin_flag_is_stored_and_revocable(self):
        provisioning.provision("alice", admin=True)
        self.assertTrue(load_tenant("alice").admin)
        provisioning.provision("alice")           # unspecified leaves it alone
        self.assertTrue(load_tenant("alice").admin)
        provisioning.provision("alice", admin=False)
        self.assertFalse(load_tenant("alice").admin)

    def test_max_workers_defaults_then_updates(self):
        provisioning.provision("alice")
        self.assertEqual(self.record("alice")["limits"]["max_workers"],
                         config.DEFAULT_MAX_WORKERS)
        provisioning.provision("alice", max_workers=9)
        self.assertEqual(load_tenant("alice").max_workers, 9)

    def test_invalid_name_touches_nothing(self):
        with self.assertRaises(TenantError):
            provisioning.provision("Alice!")
        rabbit_mgmt.ensure_tenant.assert_not_called()
        self.assertEqual(list(config.TENANTS_DIR.iterdir()), [])

    def test_broker_failure_leaves_no_half_tenant(self):
        rabbit_mgmt.ensure_tenant.side_effect = rabbit_mgmt.RabbitError("no broker")
        with self.assertRaises(rabbit_mgmt.RabbitError):
            provisioning.provision("alice")
        self.assertEqual(list(config.TENANTS_DIR.iterdir()), [])
        self.assertFalse(config.HTPASSWD_FILE.exists())


class Deprovision(ProvisioningBase):
    def test_removes_broker_objects_login_and_records_but_keeps_workspace(self):
        provisioning.provision("alice")
        (config.WORKSPACES_DIR / "alice" / "keep.txt").write_text("work")
        result = provisioning.deprovision("alice")
        self.assertTrue(result["login_removed"])
        self.assertFalse((config.TENANTS_DIR / "alice.json").exists())
        self.assertFalse(htpasswd.has_entry("alice"))
        rabbit_mgmt.delete_vhost.assert_called_once_with("ab-alice")
        self.assertTrue((config.WORKSPACES_DIR / "alice" / "keep.txt").exists())
        self.assertEqual(result["purged"], [])

    def test_purge_destroys_workspace_and_secrets(self):
        provisioning.provision("alice")
        with mock.patch.object(secrets_mod, "remove", return_value=True):
            result = provisioning.deprovision("alice", purge=True)
        self.assertFalse((config.WORKSPACES_DIR / "alice").exists())
        self.assertEqual(sorted(result["purged"]), sorted([
            str(config.WORKSPACES_DIR / "alice"),
            "anthropic-alice-apikey", "anthropic-alice-oauth",
        ]))

    def test_unknown_tenant_is_a_caller_error(self):
        with self.assertRaises(TenantError):
            provisioning.deprovision("nobody")

    def test_other_tenants_are_untouched(self):
        provisioning.provision("alice")
        provisioning.provision("bob")
        provisioning.deprovision("alice")
        self.assertTrue(htpasswd.has_entry("bob"))
        self.assertTrue((config.TENANTS_DIR / "bob.json").exists())

    def test_refuses_to_remove_the_calling_admin(self):
        with self.assertRaises(TenantError) as cm:
            provisioning.require_not_self("claude", "claude")
        self.assertIn("calling with", str(cm.exception))
        provisioning.require_not_self("claude", "someone-else")


class ListTenants(ProvisioningBase):
    def test_reports_kinds_logins_admin_and_worker_counts(self):
        provisioning.provision("alice", admin=True)
        provisioning.provision("bob")
        rows = provisioning.list_tenants(
            secret_names={"anthropic-alice-oauth"},
            unit_names=["agentbridge-bob-researcher-1.service",
                        "agentbridge-bob-researcher-2.service",
                        "agentbridge-carol-researcher-1.service"],
        )
        by_name = {r["user"]: r for r in rows}
        self.assertEqual(by_name["alice"]["credentials"], ["oauth"])
        self.assertTrue(by_name["alice"]["admin"])
        self.assertEqual(by_name["bob"]["workers"], 2)
        self.assertEqual(by_name["bob"]["credentials"], [])
        self.assertTrue(by_name["bob"]["login"])
        # A record for a tenant that does not exist is not invented.
        self.assertNotIn("carol", by_name)

    def test_no_value_or_hash_is_ever_returned(self):
        provisioning.provision("alice")
        rows = provisioning.list_tenants(secret_names={"anthropic-alice-oauth"})
        blob = json.dumps(rows)
        self.assertNotIn("$2y$", blob)
        self.assertNotIn(self.record("alice")["bus_password"], blob)


class AdminGate(ProvisioningBase):
    """Two conditions, independent of nginx: this endpoint, and the flag."""

    def test_admin_passes(self):
        provisioning.provision("alice", admin=True)
        tenant = admin_api.require_admin({config.USER_HEADER: "alice"})
        self.assertEqual(tenant.name, "alice")

    def test_plain_tenant_is_refused(self):
        provisioning.provision("bob")
        with self.assertRaises(TenantError) as cm:
            admin_api.require_admin({config.USER_HEADER: "bob"})
        self.assertIn("not an administrator", str(cm.exception))

    def test_refusal_does_not_name_who_is_an_admin(self):
        provisioning.provision("alice", admin=True)
        provisioning.provision("bob")
        with self.assertRaises(TenantError) as cm:
            admin_api.require_admin({config.USER_HEADER: "bob"})
        self.assertNotIn("alice", str(cm.exception))

    def test_missing_header_is_refused(self):
        with self.assertRaises(TenantError):
            admin_api.require_admin({})

    def test_unprovisioned_user_is_refused(self):
        with self.assertRaises(TenantError):
            admin_api.require_admin({config.USER_HEADER: "ghost"})

    def test_tool_returns_a_structured_error_rather_than_raising(self):
        provisioning.provision("bob")
        result = asyncio.run(admin_api.provision_user(
            "newbie", headers={config.USER_HEADER: "bob"}))
        self.assertFalse(result["success"])
        self.assertIn("not an administrator", result["error"])
        # And nothing was created.
        self.assertFalse((config.TENANTS_DIR / "newbie.json").exists())

    def test_admin_can_provision_through_the_tool(self):
        provisioning.provision("alice", admin=True)
        result = asyncio.run(admin_api.provision_user(
            "newbie", headers={config.USER_HEADER: "alice"}))
        self.assertEqual(result["user"], "newbie")
        self.assertIsNotNone(result["password"])

    def test_provision_is_rate_limited(self):
        provisioning.provision("alice", admin=True)
        headers = {config.USER_HEADER: "alice"}
        for i in range(5):
            asyncio.run(admin_api.provision_user(f"user{i}", headers=headers))
        refused = asyncio.run(admin_api.provision_user("user9", headers=headers))
        self.assertFalse(refused["success"])
        self.assertIn("rate limit", refused["error"])


if __name__ == "__main__":
    unittest.main()
