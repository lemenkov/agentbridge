# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Declared outputs: what counts as having produced a file.

Most of these are about the ways an agent can appear to have delivered
something without having done so — an empty file, a symlink to somewhere
else, a leftover from the previous run.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from agentbridge import expect


class Validate(unittest.TestCase):
    def test_none_and_empty_mean_nothing_declared(self):
        self.assertEqual(expect.validate(None), [])
        self.assertEqual(expect.validate([]), [])

    def test_a_plain_list_of_names(self):
        self.assertEqual(expect.validate(["a.md", "sub/b.json"]),
                         ["a.md", "sub/b.json"])

    def test_a_bare_string_is_not_a_list(self):
        # expect="review.md" would otherwise silently become 10 single letters.
        with self.assertRaises(expect.ExpectError):
            expect.validate("review.md")

    def test_absolute_and_dotdot_are_refused(self):
        for bad in ("/etc/passwd", "../outside", "sub/../../outside"):
            with self.assertRaises(expect.ExpectError):
                expect.validate([bad])

    def test_nul_and_overlong_names_are_refused(self):
        with self.assertRaises(expect.ExpectError):
            expect.validate(["a\x00b"])
        with self.assertRaises(expect.ExpectError):
            expect.validate(["x" * (expect.MAX_PATH_LENGTH + 1)])

    def test_blank_entries_are_refused(self):
        for bad in ("", "   ", None, 7):
            with self.assertRaises(expect.ExpectError):
                expect.validate([bad])

    def test_too_many_paths(self):
        with self.assertRaises(expect.ExpectError):
            expect.validate([f"f{i}" for i in range(expect.MAX_PATHS + 1)])

    def test_duplicates_collapse(self):
        self.assertEqual(expect.validate(["a.md", "a.md"]), ["a.md"])


class Check(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)

    def write(self, name, text="content"):
        path = self.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_a_written_file_is_ok(self):
        self.write("review.md")
        self.assertEqual(expect.check_one(self.out, "review.md"), expect.OK)

    def test_a_missing_file(self):
        self.assertEqual(expect.check_one(self.out, "nope.md"), expect.MISSING)

    def test_an_empty_file_is_not_a_result(self):
        self.write("review.md", "")
        self.assertEqual(expect.check_one(self.out, "review.md"), expect.EMPTY)

    def test_a_symlink_is_refused_even_when_its_target_is_fine(self):
        # An agent pointing /out/review.md at something it did not write is
        # choosing what counts as its own output.
        self.write("real.md", "actual content")
        os.symlink(self.out / "real.md", self.out / "review.md")
        self.assertEqual(expect.check_one(self.out, "review.md"), expect.SYMLINK)

    def test_a_symlink_out_of_the_directory_is_refused(self):
        os.symlink("/etc/hostname", self.out / "review.md")
        self.assertEqual(expect.check_one(self.out, "review.md"), expect.SYMLINK)

    def test_a_directory_is_not_a_file(self):
        (self.out / "review.md").mkdir()
        self.assertEqual(expect.check_one(self.out, "review.md"),
                         expect.NOT_A_FILE)

    def test_a_file_from_a_previous_run_is_stale(self):
        path = self.write("review.md")
        old = time.time() - 3600
        os.utime(path, (old, old))
        self.assertEqual(
            expect.check_one(self.out, "review.md", not_before=time.time() - 60),
            expect.STALE)

    def test_a_file_written_during_the_task_is_not_stale(self):
        self.write("review.md")
        self.assertEqual(
            expect.check_one(self.out, "review.md",
                             not_before=time.time() - 60),
            expect.OK)

    def test_a_nested_declared_output(self):
        self.write("reports/2026/review.md")
        self.assertEqual(expect.check_one(self.out, "reports/2026/review.md"),
                         expect.OK)

    def test_check_reports_every_path(self):
        self.write("good.md")
        self.write("empty.md", "")
        statuses = expect.check(self.out, ["good.md", "empty.md", "gone.md"])
        self.assertEqual(statuses, {"good.md": expect.OK,
                                    "empty.md": expect.EMPTY,
                                    "gone.md": expect.MISSING})
        self.assertFalse(expect.all_ok(statuses))

    def test_all_ok_only_when_every_one_passed(self):
        self.assertTrue(expect.all_ok({"a": expect.OK, "b": expect.OK}))
        self.assertFalse(expect.all_ok({"a": expect.OK, "b": expect.EMPTY}))
        # Nothing declared is vacuously fine.
        self.assertTrue(expect.all_ok({}))


class PromptLine(unittest.TestCase):
    def test_nothing_declared_adds_nothing(self):
        self.assertEqual(expect.prompt_line([]), "")

    def test_it_names_the_files_and_says_they_are_checked(self):
        line = expect.prompt_line(["review.md", "notes.txt"])
        self.assertIn("/out/review.md", line)
        self.assertIn("/out/notes.txt", line)
        self.assertIn("checked", line)


if __name__ == "__main__":
    unittest.main()
