# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Checkpoints and rollback.

The scripts are exercised directly against real temporary repositories —
they are shell and git, and mocking either would test nothing. In production
they run inside a leaf (I-3); the argv that puts them there is asserted
separately, and so is the reason it matters: a repository's own config can
execute a command, which the last class demonstrates rather than asserts on
faith.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge.service import checkpoint, config
from agentbridge.service.tenant import tenant_from_headers

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
GIT = shutil.which("git")


def git(repo: Path, *args: str, check=True) -> str:
    out = subprocess.run(
        [GIT, "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )
    return out.stdout.strip()


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(DEPLOY / name), *args],
        capture_output=True, text=True, check=False,
    )


@unittest.skipUnless(GIT, "git is required")
class Scripts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.repo = self.root / "proj"
        self.repo.mkdir(parents=True)
        git(self.repo, "init", "-q", ".")
        (self.repo / "a.txt").write_text("one\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "first")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def checkpoint(self, task_id="abc123"):
        done = run_script("checkpoint.sh", task_id, str(self.root))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def rollback(self, task_id="abc123", only="ALL", force="no"):
        done = run_script("rollback.sh", task_id, str(self.root), only, force)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def agent_commits(self, text="two\n", message="agent work"):
        (self.repo / "b.txt").write_text(text)
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    # --- checkpoint -------------------------------------------------------

    def test_it_records_the_ref_the_branch_and_cleanliness(self):
        result = self.checkpoint()
        self.assertEqual(result["task_id"], "abc123")
        [row] = result["repos"]
        self.assertEqual(row["repo"], "proj")
        self.assertEqual(row["head"], self.base)
        self.assertIn(row["branch"], ("master", "main"))
        self.assertFalse(row["dirty"])
        self.assertEqual(
            git(self.repo, "rev-parse", "refs/agentbridge/pre/abc123"), self.base)

    def test_the_sidecar_remembers_the_branch(self):
        self.checkpoint()
        sidecar = json.loads(
            (self.repo / ".git" / "agentbridge" / "abc123.json").read_text())
        self.assertEqual(sidecar["head"], self.base)
        self.assertIn(sidecar["branch"], ("master", "main"))
        self.assertFalse(sidecar["dirty"])

    def test_a_dirty_worktree_is_recorded_as_such(self):
        (self.repo / "a.txt").write_text("edited\n")
        [row] = self.checkpoint()["repos"]
        self.assertTrue(row["dirty"])

    def test_every_repository_in_the_workspace(self):
        other = self.root / "nested" / "second"
        other.mkdir(parents=True)
        git(other, "init", "-q", ".")
        (other / "x").write_text("x")
        git(other, "add", "x")
        git(other, "commit", "-q", "-m", "x")
        repos = {r["repo"] for r in self.checkpoint()["repos"]}
        self.assertEqual(repos, {"proj", "nested/second"})

    def test_a_directory_that_is_not_a_repository_is_ignored(self):
        (self.root / "notes").mkdir()
        (self.root / "notes" / "a.md").write_text("hi")
        self.assertEqual(len(self.checkpoint()["repos"]), 1)

    def test_an_empty_workspace_yields_no_repos(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        done = run_script("checkpoint.sh", "abc123", str(empty))
        self.assertEqual(json.loads(done.stdout)["repos"], [])

    # --- rollback ---------------------------------------------------------

    def test_it_restores_head_and_keeps_the_discarded_tip(self):
        self.checkpoint()
        after = self.agent_commits()
        [row] = self.rollback()["repos"]
        self.assertEqual(row["status"], "reset")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        # The rollback is itself undoable.
        self.assertEqual(
            git(self.repo, "rev-parse", "refs/agentbridge/post/abc123"), after)

    def test_untracked_files_survive(self):
        self.checkpoint()
        self.agent_commits()
        (self.repo / "keep.txt").write_text("the only copy\n")
        self.rollback()
        self.assertTrue((self.repo / "keep.txt").exists())
        self.assertEqual((self.repo / "keep.txt").read_text(), "the only copy\n")

    def test_a_checkpoint_taken_while_dirty_is_refused(self):
        (self.repo / "a.txt").write_text("uncommitted work\n")
        self.checkpoint()
        [row] = self.rollback()["repos"]
        self.assertEqual(row["status"], "refused_dirty")
        # And nothing was touched.
        self.assertEqual((self.repo / "a.txt").read_text(), "uncommitted work\n")

    def test_force_overrides_the_dirty_refusal(self):
        (self.repo / "a.txt").write_text("uncommitted work\n")
        self.checkpoint()
        [row] = self.rollback(force="force")["repos"]
        self.assertEqual(row["status"], "reset")
        self.assertEqual((self.repo / "a.txt").read_text(), "one\n")

    def test_an_unknown_task_has_no_checkpoint(self):
        self.checkpoint("abc123")
        [row] = self.rollback("999999")["repos"]
        self.assertEqual(row["status"], "no_checkpoint")

    def test_one_repository_can_be_singled_out(self):
        other = self.root / "second"
        other.mkdir()
        git(other, "init", "-q", ".")
        (other / "x").write_text("x")
        git(other, "add", "x")
        git(other, "commit", "-q", "-m", "x")
        other_base = git(other, "rev-parse", "HEAD")
        self.checkpoint()
        self.agent_commits()
        (other / "y").write_text("y")
        git(other, "add", "y")
        git(other, "commit", "-q", "-m", "y")

        repos = self.rollback(only="proj")["repos"]
        self.assertEqual([r["repo"] for r in repos], ["proj"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        # The repository not named is untouched.
        self.assertNotEqual(git(other, "rev-parse", "HEAD"), other_base)

    def test_a_branch_checked_out_elsewhere_moves_without_the_worktree(self):
        self.checkpoint()
        after = self.agent_commits()
        recorded = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "elsewhere")
        [row] = self.rollback()["repos"]
        self.assertEqual(row["status"], "branch_reset")
        self.assertEqual(git(self.repo, "rev-parse", recorded), self.base)
        # The checked-out branch keeps the work.
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), after)

    def test_rollback_is_idempotent_enough_to_repeat(self):
        self.checkpoint()
        self.agent_commits()
        self.rollback()
        [row] = self.rollback()["repos"]
        self.assertEqual(row["status"], "reset")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)


class LeafArgv(unittest.TestCase):
    """I-3: these git commands must run in a container, with nothing to steal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for patch in (
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "WORKSPACES_DIR", root / "ws"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config.TENANTS_DIR.mkdir()
        (config.TENANTS_DIR / "alice.json").write_text(json.dumps({
            "vhost": "ab-alice", "bus_user": "ab-alice",
            "bus_password": "s3cr3t", "limits": {"max_workers": 2},
        }))
        self.alice = tenant_from_headers({config.USER_HEADER: "alice"})

    def test_the_checkpoint_runs_in_a_container(self):
        argv = checkpoint.checkpoint_command(self.alice, "implementer")
        self.assertEqual(argv[:2], ["podman", "run"])
        self.assertIn("--network=none", argv)
        self.assertIn("{task_id}", argv)

    def test_no_credential_no_claude_no_bus_url(self):
        for argv in (
            checkpoint.checkpoint_command(self.alice, "implementer"),
            checkpoint.rollback_command(self.alice, "implementer", "abc123",
                                        "ALL", False),
        ):
            joined = " ".join(argv)
            self.assertNotIn("--secret", joined)
            self.assertNotIn("claude", joined)
            self.assertNotIn("ANTHROPIC", joined)
            self.assertNotIn("s3cr3t", joined)
            self.assertNotIn("amqp://", joined)

    def test_the_script_is_mounted_read_only_from_outside_the_workspace(self):
        argv = checkpoint.checkpoint_command(self.alice, "implementer")
        mount = next(a for a in argv if a.endswith("/script.sh:ro"))
        source = mount.split(":")[0]
        # What executes must not live where the agent can rewrite it.
        self.assertFalse(source.startswith(str(self.alice.workspace)))
        self.assertTrue(source.endswith("deploy/checkpoint.sh"))

    def test_the_workspace_is_writable_because_refs_must_be_written(self):
        argv = checkpoint.checkpoint_command(self.alice, "implementer")
        self.assertIn(f"{self.alice.workspace}:/workspace:rw", argv)

    def test_the_force_flag_is_passed_as_a_word_not_a_shell_fragment(self):
        argv = checkpoint.rollback_command(self.alice, "implementer", "abc123",
                                           "ALL", True)
        self.assertEqual(argv[-4:], ["abc123", "/workspace", "ALL", "force"])

    def test_task_ids_and_repo_paths_are_validated(self):
        for bad in ("", "../../etc", "abc123; rm -rf /", "ABC123", None):
            with self.assertRaises(checkpoint.CheckpointError):
                checkpoint.validate_task_id(bad)
        self.assertEqual(checkpoint.validate_repo(None), "ALL")
        self.assertEqual(checkpoint.validate_repo("nested/second"),
                         "nested/second")
        for bad in ("/abs", "../up", "a/../../b", "x" * 200):
            with self.assertRaises(checkpoint.CheckpointError):
                checkpoint.validate_repo(bad)

    def test_the_role_decides_the_image(self):
        argv = checkpoint.checkpoint_command(self.alice, "implementer")
        self.assertIn("agentbridge-pydev", argv)

    def test_a_worker_id_maps_to_its_role(self):
        self.assertEqual(checkpoint.role_for_worker("implementer-2"),
                         "implementer")
        # An unknown id still gets a read-write role, since writing refs is
        # the whole job.
        self.assertEqual(checkpoint.role_for_worker("mystery-9"), "implementer")


@unittest.skipUnless(GIT, "git is required")
class WhyNotOnTheHost(unittest.TestCase):
    """The I-3 regression test: a repository is a program.

    A read-write agent can put `core.fsmonitor` in its own repository's
    config, and git runs it. These two tests show the hazard is real on this
    git, and that the scripts suppress it — but suppression is defence in
    depth, not the defence: the container is.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.repo = self.root / "proj"
        self.repo.mkdir(parents=True)
        self.marker = Path(self._tmp.name) / "EXECUTED"
        hook = self.repo / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {self.marker}\nexit 0\n")
        hook.chmod(0o755)
        git(self.repo, "init", "-q", ".")
        (self.repo / "a.txt").write_text("one\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "first")
        git(self.repo, "config", "core.fsmonitor", str(hook))

    def test_plain_git_in_such_a_repository_executes_its_command(self):
        # This is what would happen if the service ran git on the host.
        git(self.repo, "status", "--porcelain", check=False)
        self.assertTrue(self.marker.exists(),
                        "expected git to run the repository's fsmonitor")

    def test_the_checkpoint_script_does_not(self):
        done = run_script("checkpoint.sh", "abc123", str(self.root))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.marker.exists())
        # And it still did its job.
        self.assertEqual(len(json.loads(done.stdout)["repos"]), 1)

    def test_the_rollback_script_does_not(self):
        run_script("checkpoint.sh", "abc123", str(self.root))
        self.marker.unlink(missing_ok=True)
        done = run_script("rollback.sh", "abc123", str(self.root))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
