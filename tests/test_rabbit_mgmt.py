# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Management-API provisioning, against a fake transport.

The paths and bodies are asserted exactly: a wrong body here would silently
grant a tenant the wrong permissions, which no later test would notice.
"""

import base64
import hashlib
import unittest

import httpx

from agentbridge.service import rabbit_mgmt


def fake_api(handler):
    """A client whose requests go to ``handler`` instead of the network."""
    return httpx.Client(
        base_url="http://api.test/api",
        auth=httpx.BasicAuth("admin", "pw"),
        transport=httpx.MockTransport(handler),
    )


class Recorder:
    """Records requests and replies 2xx, or a canned status per path."""

    def __init__(self, statuses=None, bodies=None):
        self.calls = []
        self.statuses = statuses or {}
        self.bodies = bodies or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content) if request.content else None
        # raw_path, not path: it keeps the percent-encoding, which is the whole
        # point for a vhost named "/".
        path = request.url.raw_path.decode().split("?", 1)[0]
        self.calls.append((request.method, path, body))
        status = self.statuses.get(path, 204)
        return httpx.Response(status, json=self.bodies.get(path))


class PasswordHash(unittest.TestCase):
    def test_round_trips(self):
        """Decode the stored form and recompute it: 4-byte salt, then sha256."""
        stored = base64.b64decode(rabbit_mgmt.password_hash("hunter2"))
        salt, digest = stored[:4], stored[4:]
        self.assertEqual(len(stored), 36)
        self.assertEqual(digest, hashlib.sha256(salt + b"hunter2").digest())

    def test_salt_differs_per_call(self):
        self.assertNotEqual(rabbit_mgmt.password_hash("x"),
                            rabbit_mgmt.password_hash("x"))


class Provisioning(unittest.TestCase):
    def test_ensure_tenant_sends_exact_paths_and_bodies(self):
        rec = Recorder()
        with fake_api(rec) as api:
            rabbit_mgmt.ensure_tenant("ab-alice", "ab-alice", "bus-pw", api=api)

        self.assertEqual([(m, p) for m, p, _ in rec.calls], [
            ("PUT", "/api/vhosts/ab-alice"),
            ("PUT", "/api/users/ab-alice"),
            ("PUT", "/api/permissions/ab-alice/ab-alice"),
        ])
        user_body = rec.calls[1][2]
        self.assertEqual(user_body["hashing_algorithm"],
                         "rabbit_password_hashing_sha256")
        self.assertEqual(user_body["tags"], "")
        self.assertNotIn("password", user_body)
        self.assertEqual(rec.calls[2][2],
                         {"configure": ".*", "write": ".*", "read": ".*"})

    def test_password_never_sent_in_clear(self):
        rec = Recorder()
        with fake_api(rec) as api:
            rabbit_mgmt.ensure_user("ab-alice", "bus-pw", api=api)
        self.assertNotIn("bus-pw", str(rec.calls))

    def test_vhost_name_is_percent_encoded(self):
        rec = Recorder()
        with fake_api(rec) as api:
            rabbit_mgmt.ensure_vhost("/", api=api)
        self.assertEqual(rec.calls[0][1], "/api/vhosts/%2F")

    def test_non_2xx_raises(self):
        rec = Recorder(statuses={"/api/vhosts/ab-bob": 500})
        with fake_api(rec) as api, self.assertRaises(rabbit_mgmt.RabbitError) as cm:
            rabbit_mgmt.ensure_vhost("ab-bob", api=api)
        self.assertIn("500", str(cm.exception))

    def test_delete_tolerates_missing(self):
        rec = Recorder(statuses={"/api/users/gone": 404, "/api/vhosts/gone": 404})
        with fake_api(rec) as api:
            self.assertFalse(rabbit_mgmt.delete_user("gone", api=api))
            self.assertFalse(rabbit_mgmt.delete_vhost("gone", api=api))

    def test_delete_reports_removal(self):
        rec = Recorder(statuses={"/api/users/alice": 204})
        with fake_api(rec) as api:
            self.assertTrue(rabbit_mgmt.delete_user("alice", api=api))

    def test_queues_maps_fields(self):
        rec = Recorder(statuses={"/api/queues/ab-alice": 200}, bodies={
            "/api/queues/ab-alice": [
                {"name": "research.work", "messages_ready": 2,
                 "messages_unacknowledged": 1, "consumers": 3},
            ]})
        with fake_api(rec) as api:
            self.assertEqual(rabbit_mgmt.queues("ab-alice", api=api), [
                {"name": "research.work", "ready": 2, "unacked": 1, "consumers": 3},
            ])

    def test_queues_of_unknown_vhost_is_empty(self):
        rec = Recorder(statuses={"/api/queues/nope": 404})
        with fake_api(rec) as api:
            self.assertEqual(rabbit_mgmt.queues("nope", api=api), [])

    def test_unreachable_broker_raises_without_credentials(self):
        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        with fake_api(refuse) as api, self.assertRaises(rabbit_mgmt.RabbitError) as cm:
            rabbit_mgmt.ensure_vhost("ab-alice", api=api)
        self.assertNotIn("pw", str(cm.exception).replace("api.test", ""))


class Credentials(unittest.TestCase):
    def test_missing_admin_credentials_name_the_file(self):
        from unittest import mock

        from agentbridge.service import config

        with mock.patch.object(rabbit_mgmt, "_client", None), \
             mock.patch.object(config, "RABBITMQ_ADMIN_USER", ""), \
             mock.patch.object(config, "RABBITMQ_ADMIN_PASSWORD", ""), \
             self.assertRaises(rabbit_mgmt.RabbitError) as cm:
            rabbit_mgmt.client()
        self.assertIn(config.SYSCONFIG_PATH, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
