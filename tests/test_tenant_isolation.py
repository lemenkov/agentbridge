# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""One tenant must not see another tenant's messages.

The claim this file defends is narrow and worth stating exactly, because the
two halves of it are enforced by different things and fail differently.

**The broker enforces the bus boundary.** Each tenant gets its own vhost, its
own bus user, and permissions on that vhost alone. A vhost is a hard namespace
in RabbitMQ: exchanges, queues and bindings do not exist across it, so `*.output`
in one tenant's vhost cannot bind to anything in another's — there is no
routing key to spell. No code of ours is between a tenant and that boundary,
which is why it is the half that cannot be broken by a bug here.

**The service chooses which credentials to use.** That half is ours. The
tenant comes from a header nginx sets, and every bus operation builds its
connection from that tenant's own AMQP URL. A bug that picked the wrong
tenant's URL would be a cross-tenant bug, and no vhost would stop it — the
credentials would already be the other tenant's. So the tests below check the
*choice*, not just the boundary.

**What is deliberately not separated** — written down here so it is not
discovered later as a surprise: every tenant's files live under one
`TENANTS_DIR` owned by the single service user, and worker units are
name-prefixed inside one `systemd --user` instance. The OS does not separate
tenants. Anything running as the service user can read every tenant's record.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge.service import broker, config, provisioning, rabbit_mgmt
from agentbridge.service.tenant import tenant_from_headers


class IsolationBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for patch in (
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "WORKSPACES_DIR", root / "ws"),
            mock.patch.object(config, "HTPASSWD_FILE", root / "htpasswd" / "users"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config.TENANTS_DIR.mkdir()


class BrokerBoundary(IsolationBase):
    """What provisioning asks the broker for, per tenant."""

    def setUp(self):
        super().setUp()
        self.calls = []
        # Record what ensure_tenant would do, by running the real thing
        # against a fake management API: the point is the argv it builds, not
        # the HTTP.
        def fake_request(method, path, json=None, ok_missing=False, api=None):
            self.calls.append((method, path, json))
            return mock.Mock(status_code=200, json=list)

        patch = mock.patch.object(rabbit_mgmt, "_request", fake_request)
        patch.start()
        self.addCleanup(patch.stop)
        provisioning.provision("alice")
        provisioning.provision("bob")

    def permissions(self) -> dict[str, str]:
        """vhost -> user, from the PUT /permissions/<vhost>/<user> calls."""
        out = {}
        for method, path, _ in self.calls:
            if method == "PUT" and path.startswith("/permissions/"):
                _, _, vhost, user = path.split("/")
                out[vhost] = user
        return out

    def test_each_tenant_gets_its_own_vhost_and_bus_user(self):
        self.assertEqual(self.permissions(), {"ab-alice": "ab-alice",
                                              "ab-bob": "ab-bob"})

    def test_no_tenant_is_granted_rights_on_another_vhost(self):
        for method, path, _ in self.calls:
            if method == "PUT" and path.startswith("/permissions/"):
                _, _, vhost, user = path.split("/")
                self.assertEqual(vhost, f"ab-{user.removeprefix('ab-')}",
                                 f"{user} granted rights on {vhost}")

    def test_the_grant_is_scoped_to_the_vhost_not_to_queue_names(self):
        # `.*` inside one vhost is intentional and is not a weakness: a vhost
        # is the namespace, so "everything here" means everything of this
        # tenant's. What would be a weakness is a permission row on a vhost
        # the tenant does not own, which the test above rules out.
        for method, path, body in self.calls:
            if method == "PUT" and path.startswith("/permissions/"):
                self.assertEqual(body, {"configure": ".*", "write": ".*",
                                        "read": ".*"})

    def test_no_tenant_carries_a_broker_tag(self):
        # A `management` or `administrator` tag would let a tenant log into
        # the management UI and read every other vhost, going around the
        # permission rows entirely.
        users = [(path, body) for method, path, body in self.calls
                 if method == "PUT" and path.startswith("/users/")]
        self.assertEqual(len(users), 2)
        for path, body in users:
            self.assertEqual(body["tags"], "", f"{path} has broker tags")

    def test_bus_passwords_differ_between_tenants(self):
        alice = json.loads(
            (config.TENANTS_DIR / "alice.json").read_text())["bus_password"]
        bob = json.loads(
            (config.TENANTS_DIR / "bob.json").read_text())["bus_password"]
        self.assertNotEqual(alice, bob)
        self.assertGreater(len(alice), 20)

    def test_a_password_is_never_sent_in_the_clear(self):
        # The management API is told a hash, so the broker never holds the
        # value either.
        for method, path, body in self.calls:
            if method == "PUT" and path.startswith("/users/"):
                self.assertIn("password_hash", body)
                self.assertNotIn("password", body)


class ServiceChoice(IsolationBase):
    """The half that is ours: which credentials a request ends up using."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(rabbit_mgmt, "ensure_tenant")
        patch.start()
        self.addCleanup(patch.stop)
        provisioning.provision("alice")
        provisioning.provision("bob")

    def connect_url_for(self, user: str) -> str:
        """The AMQP URL a bus operation opens when the header says `user`."""
        tenant = tenant_from_headers({config.USER_HEADER: user})
        opened = []

        class FakeConn:
            async def channel(self):
                channel = mock.AsyncMock()
                channel.declare_exchange = mock.AsyncMock(
                    return_value=mock.AsyncMock())
                return channel

            async def close(self):
                pass

        async def fake_connect(url, **kwargs):
            opened.append(url)
            return FakeConn()

        async def run():
            with mock.patch.object(broker.aio_pika, "connect", fake_connect):
                async with broker._channel(tenant):
                    pass

        asyncio.run(run())
        self.assertEqual(len(opened), 1)
        return opened[0]

    def test_the_connection_carries_the_header_s_tenant_and_no_other(self):
        alice = self.connect_url_for("alice")
        bob = self.connect_url_for("bob")
        self.assertIn("/ab-alice", alice)
        self.assertIn("ab-alice:", alice)
        self.assertNotIn("ab-bob", alice)
        self.assertIn("/ab-bob", bob)
        self.assertNotIn("ab-alice", bob)

    def test_the_vhost_is_in_the_url_so_there_is_nothing_to_override(self):
        # The vhost is part of the credential, not a parameter of each
        # operation: no bus call takes a vhost argument, so none can be given
        # the wrong one.
        url = self.connect_url_for("alice")
        self.assertTrue(url.endswith("/ab-alice"), url)

    def test_an_unknown_user_gets_no_credentials_at_all(self):
        from agentbridge.service.tenant import TenantError
        with self.assertRaises(TenantError):
            tenant_from_headers({config.USER_HEADER: "ghost"})
        with self.assertRaises(TenantError):
            tenant_from_headers({})

    def test_a_tenant_cannot_name_another_tenant_through_the_header(self):
        # The header is set by nginx from $remote_user and overwrites whatever
        # the client sent (deploy/nginx-location.conf). Here the point is
        # narrower: the service trusts exactly one header and derives
        # everything from it, so there is no second place to look.
        tenant = tenant_from_headers({config.USER_HEADER: "bob",
                                      "X-Tenant": "alice",
                                      "X-Forwarded-For": "alice"})
        self.assertEqual(tenant.name, "bob")
        self.assertEqual(tenant.vhost, "ab-bob")


class SharedByDesign(IsolationBase):
    """The limits of the claim, asserted so they stay deliberate."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(rabbit_mgmt, "ensure_tenant")
        patch.start()
        self.addCleanup(patch.stop)
        provisioning.provision("alice")
        provisioning.provision("bob")

    def test_records_share_one_directory_owned_by_the_service_user(self):
        # Not a finding — a documented consequence. OS-level separation would
        # need a uid per tenant, which is a different design.
        alice = tenant_from_headers({config.USER_HEADER: "alice"})
        bob = tenant_from_headers({config.USER_HEADER: "bob"})
        self.assertEqual(alice.amqp_url_file.parent, bob.amqp_url_file.parent)

    def test_but_each_record_is_private_to_that_user(self):
        for name in ("alice", "bob"):
            mode = (config.TENANTS_DIR / f"{name}.json").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"{name}.json is not 0600")

    def test_workspaces_do_not_overlap(self):
        alice = tenant_from_headers({config.USER_HEADER: "alice"})
        bob = tenant_from_headers({config.USER_HEADER: "bob"})
        self.assertNotEqual(alice.workspace, bob.workspace)
        self.assertFalse(
            str(alice.workspace).startswith(str(bob.workspace) + "/"))
        self.assertEqual(alice.workspace.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
