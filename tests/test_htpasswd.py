# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The service's own password file: atomic writes, other lines untouched."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bcrypt

from agentbridge.service import config, htpasswd
from agentbridge.service.tenant import TenantError

OTHER = "bob:$2y$12$abcdefghijklmnopqrstuv0123456789012345678901234567890"


class HtpasswdFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "htpasswd"
        self.dir.mkdir()
        self.path = self.dir / "users"
        patch = mock.patch.object(config, "HTPASSWD_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def lines(self):
        return self.path.read_text().splitlines()

    def test_entry_is_bcrypt_and_verifies(self):
        htpasswd.set_entry("alice", "a-long-enough-password")
        name, digest = self.lines()[0].split(":", 1)
        self.assertEqual(name, "alice")
        self.assertTrue(digest.startswith("$2y$"), digest[:4])
        # nginx accepts $2y$; bcrypt itself only verifies $2b$, and the two are
        # the same format, so swap the prefix back to check the hash is real.
        self.assertTrue(bcrypt.checkpw(b"a-long-enough-password",
                                       ("$2b$" + digest[4:]).encode()))

    def test_password_is_not_stored_in_clear(self):
        htpasswd.set_entry("alice", "a-long-enough-password")
        self.assertNotIn("a-long-enough-password", self.path.read_text())

    def test_replacing_keeps_other_lines_byte_for_byte(self):
        self.path.write_text(OTHER + "\n")
        htpasswd.set_entry("alice", "first-password-here")
        first = next(ln for ln in self.lines() if ln.startswith("alice:"))
        htpasswd.set_entry("alice", "second-password-xyz")
        self.assertIn(OTHER, self.lines())
        self.assertEqual(len([ln for ln in self.lines() if ln.startswith("alice:")]), 1)
        self.assertNotIn(first, self.lines())

    def test_remove_reports_whether_there_was_an_entry(self):
        htpasswd.set_entry("alice", "a-long-enough-password")
        self.assertTrue(htpasswd.remove_entry("alice"))
        self.assertFalse(htpasswd.remove_entry("alice"))
        self.assertFalse(htpasswd.has_entry("alice"))

    def test_entries_lists_names_only(self):
        self.path.write_text(OTHER + "\n")
        htpasswd.set_entry("alice", "a-long-enough-password")
        self.assertEqual(htpasswd.entries(), ["alice", "bob"])

    def test_short_password_refused_and_nothing_written(self):
        with self.assertRaises(TenantError):
            htpasswd.set_entry("alice", "short")
        self.assertFalse(self.path.exists())

    def test_invalid_name_refused(self):
        for name in ("Alice", "al ice", "", "a" * 40, "../etc/passwd"):
            with self.subTest(name=name), self.assertRaises(TenantError):
                htpasswd.set_entry(name, "a-long-enough-password")

    def test_mode_and_group_preserved_on_rewrite(self):
        self.path.write_text(OTHER + "\n")
        os.chmod(self.path, 0o640)
        before = self.path.stat()
        htpasswd.set_entry("alice", "a-long-enough-password")
        after = self.path.stat()
        self.assertEqual(after.st_mode & 0o7777, before.st_mode & 0o7777)
        self.assertEqual(after.st_gid, before.st_gid)

    def test_previous_generation_kept_as_bak(self):
        htpasswd.set_entry("alice", "a-long-enough-password")
        htpasswd.set_entry("bob", "another-password-ok")
        backup = self.path.with_name("users.bak")
        self.assertTrue(backup.exists())
        self.assertNotIn("bob", backup.read_text())

    def test_no_temp_file_left_behind_on_failure(self):
        self.path.write_text(OTHER + "\n")
        with mock.patch.object(htpasswd.os, "replace",
                               side_effect=OSError("disk full")), \
             self.assertRaises(OSError):
            htpasswd.set_entry("alice", "a-long-enough-password")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         [".lock", "users"])
        self.assertEqual(self.path.read_text(), OTHER + "\n")

    def test_created_file_is_not_world_readable(self):
        htpasswd.set_entry("alice", "a-long-enough-password")
        self.assertEqual(self.path.stat().st_mode & 0o077, 0o040)

    def test_generated_password_is_alphanumeric_and_long(self):
        pw = htpasswd.generate_password()
        self.assertGreaterEqual(len(pw), htpasswd.MIN_PASSWORD_LENGTH)
        self.assertTrue(pw.isalnum())
        self.assertNotEqual(pw, htpasswd.generate_password())


if __name__ == "__main__":
    unittest.main()
