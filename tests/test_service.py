# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge.service import config, workers, workspace
from agentbridge.service.roles import ROLES
from agentbridge.service.tenant import (
    TenantError,
    pick_credential,
    tenant_from_headers,
)
from agentbridge.service.workspace import WorkspaceError

PASSWORD = "s3cr3t-bus-password"


class TenantDirs(unittest.TestCase):
    """Point the service's tenant and workspace dirs at a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.patches = [
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "WORKSPACES_DIR", root / "ws"),
        ]
        for p in self.patches:
            p.start()
        config.TENANTS_DIR.mkdir()
        (config.WORKSPACES_DIR / "alice").mkdir(parents=True)
        (config.TENANTS_DIR / "alice.json").write_text(json.dumps({
            "vhost": "ab-alice", "bus_user": "ab-alice",
            "bus_password": PASSWORD, "limits": {"max_workers": 2},
        }))
        self.alice = tenant_from_headers({config.USER_HEADER: "alice"})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()


class TenantResolution(TenantDirs):
    def test_header_resolves_tenant(self):
        self.assertEqual(self.alice.name, "alice")
        self.assertEqual(self.alice.vhost, "ab-alice")
        self.assertEqual(self.alice.max_workers, 2)
        self.assertEqual(self.alice.workspace, config.WORKSPACES_DIR / "alice")

    def test_missing_header_has_no_default(self):
        for headers in ({}, {config.USER_HEADER: ""}, None):
            with self.assertRaisesRegex(TenantError, "header is missing"):
                tenant_from_headers(headers)

    def test_bad_names_rejected(self):
        for name in ("../etc", "Alice", "a b", "-x", "a" * 33, "alice/x"):
            with self.assertRaisesRegex(TenantError, "invalid user name"):
                tenant_from_headers({config.USER_HEADER: name})

    def test_unprovisioned_user(self):
        with self.assertRaisesRegex(TenantError, "not provisioned"):
            tenant_from_headers({config.USER_HEADER: "bob"})

    def test_credential_preference(self):
        both = {"anthropic-alice-apikey", "anthropic-alice-oauth"}
        self.assertEqual(pick_credential(self.alice, both),
                         ("anthropic-alice-apikey", "ANTHROPIC_API_KEY"))
        self.assertEqual(pick_credential(self.alice, {"anthropic-alice-oauth"}),
                         ("anthropic-alice-oauth", "CLAUDE_CODE_OAUTH_TOKEN"))
        # another tenant's secret never counts
        self.assertIsNone(pick_credential(self.alice, {"anthropic-bob-apikey"}))


class WorkspacePaths(TenantDirs):
    def test_inside_ok(self):
        root = self.alice.workspace.resolve()
        self.assertEqual(workspace.resolve(self.alice, "."), root)
        self.assertEqual(workspace.resolve(self.alice, "a/b.txt"), root / "a/b.txt")

    def test_escapes_rejected(self):
        for path in ("/etc/passwd", "../bob", "a/../../x"):
            with self.assertRaises(WorkspaceError):
                workspace.resolve(self.alice, path)

    def test_symlink_escape_rejected(self):
        os.symlink("/etc", self.alice.workspace / "evil")
        with self.assertRaisesRegex(WorkspaceError, "outside"):
            workspace.resolve(self.alice, "evil/passwd")

    def test_write_read_roundtrip(self):
        workspace.write(self.alice, "notes/x.md", "hello")
        self.assertEqual(workspace.read(self.alice, "notes/x.md", 100)["text"], "hello")
        self.assertTrue(workspace.read(self.alice, "notes/x.md", 2)["truncated"])

    def test_clone_rejects_non_https(self):
        for url in ("file:///etc", "ssh://host/repo", "git://host/repo"):
            with self.assertRaises(WorkspaceError):
                asyncio.run(workspace.clone(self.alice, url, "repo"))


class WorkerCommand(TenantDirs):
    def build(self, role="researcher", pool=False):
        cred = ("anthropic-alice-apikey", "ANTHROPIC_API_KEY")
        with mock.patch.object(config, "CLAUDE_BIN", "/usr/local/libexec/agentbridge/claude"):
            return workers.build_command(self.alice, role, 3, cred, pool)

    def test_no_secret_in_argv(self):
        argv = " ".join(self.build())
        self.assertNotIn(PASSWORD, argv)
        self.assertNotIn("amqp://", argv)

    def test_shape(self):
        cmd = self.build()
        self.assertEqual(cmd[:4], ["systemd-run", "--user", "--quiet", "--collect"])
        self.assertIn("--unit=agentbridge-alice-researcher-3.service", cmd)
        self.assertNotIn("--", cmd)                   # bridge execs argv verbatim
        self.assertNotIn("--network=host", cmd)       # leaves can't reach loopback
        self.assertIn("--cgroups=split", cmd)         # containers inside the unit
        self.assertIn("anthropic-alice-apikey,type=env,target=ANTHROPIC_API_KEY", cmd)
        self.assertIn(f"{config.WORKSPACES_DIR / 'alice'}:/workspace:ro", cmd)
        self.assertEqual(cmd[-4:], ["agentbridge-base", "claude", "--print",
                                    "--dangerously-skip-permissions"])
        self.assertNotIn("--work-queue", cmd)

    def test_role_decides_mounts_and_pool(self):
        (config.TENANTS_DIR / "alice.gitconfig").write_text("[user]\n")
        cmd = self.build("implementer", pool=True)
        self.assertIn(f"{config.WORKSPACES_DIR / 'alice'}:/workspace:rw", cmd)
        self.assertIn(f"{config.TENANTS_DIR / 'alice.gitconfig'}:/root/.gitconfig:ro", cmd)
        self.assertIn("implementer.work", cmd)

    def test_next_index(self):
        ids = ["researcher-1", "researcher-4", "implementer-9"]
        self.assertEqual(workers.next_index(ids, "researcher"), 5)
        self.assertEqual(workers.next_index(ids, "reviewer"), 1)

    def test_list_units_only_own_prefix(self):
        units = [
            {"unit": "agentbridge-alice-researcher-1.service", "active": "active", "sub": "running"},
            {"unit": "agentbridge-alicex-researcher-1.service", "active": "active", "sub": "running"},
            {"unit": "agentbridge-bob-researcher-1.service", "active": "active", "sub": "running"},
        ]
        with mock.patch.object(workers, "_run",
                               mock.AsyncMock(return_value=(0, json.dumps(units), ""))):
            got = asyncio.run(workers.list_units(self.alice))
        self.assertEqual([w["id"] for w in got], ["researcher-1"])


class Roles(unittest.TestCase):
    def test_preambles_exist(self):
        for name, role in ROLES.items():
            for path in role.preamble_files:
                self.assertTrue(path.is_file(), f"{name}: missing {path}")
            self.assertIn(role.workspace, ("ro", "rw"))

    def test_images_have_containerfiles(self):
        images = config.AGENTBRIDGE_HOME / "images"
        for role in ROLES.values():
            name = role.image.removeprefix("agentbridge-")
            self.assertTrue((images / f"{name}.Containerfile").is_file(), role.image)


if __name__ == "__main__":
    unittest.main()
