# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Git credentials.

Most of this is about where the secret is *not*: not in argv, not in a URL,
not in a result, not in a log line, and not offered to a host or a scope that
did not ask for it.
"""

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge.service import config, gitcred
from agentbridge.service.tenant import tenant_from_headers

TOKEN = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123"


class GitCredBase(unittest.TestCase):
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
            "bus_password": "p", "limits": {"max_workers": 2},
        }))
        self.alice = tenant_from_headers({config.USER_HEADER: "alice"})


class Validation(GitCredBase):
    def test_only_allowlisted_hosts(self):
        self.assertEqual(gitcred.validate_host("GitHub.com"), "github.com")
        for bad in ("evil.example", "", "github.com.evil.example"):
            with self.assertRaises(gitcred.GitCredError):
                gitcred.validate_host(bad)

    def test_match_shapes(self):
        self.assertEqual(gitcred.validate_match("owner/name"), "owner/name")
        self.assertEqual(gitcred.validate_match("owner/*"), "owner/*")
        for bad in ("owner", "/name", "owner/name/extra", "../etc", "o w/n"):
            with self.assertRaises(gitcred.GitCredError):
                gitcred.validate_match(bad)

    def test_a_wildcard_can_be_refused_where_it_does_not_belong(self):
        with self.assertRaises(gitcred.GitCredError):
            gitcred.validate_match("owner/*", allow_wildcard=False)

    def test_token_prefixes(self):
        self.assertIn("fine-grained", gitcred.describe_token(TOKEN))
        self.assertIn("discouraged",
                      gitcred.describe_token("ghp_" + "x" * 36))
        self.assertIn("gitlab", gitcred.describe_token("glpat-" + "x" * 24))

    def test_an_unrecognised_token_is_refused_without_quoting_it(self):
        with self.assertRaises(gitcred.GitCredError) as cm:
            gitcred.describe_token("hunter2hunter2hunter2hunter2")
        self.assertNotIn("hunter2", str(cm.exception))

    def test_a_short_token_does_not_have_its_length_reported(self):
        with self.assertRaises(gitcred.GitCredError) as cm:
            gitcred.describe_token("ghp_short")
        # The length of a secret is itself a hint.
        self.assertNotIn("9", str(cm.exception))

    def test_whitespace_is_refused(self):
        with self.assertRaises(gitcred.GitCredError):
            gitcred.describe_token(TOKEN[:20] + " " + TOKEN[20:])


class Urls(GitCredBase):
    def test_https_urls_parse(self):
        for url in ("https://github.com/owner/name",
                    "https://github.com/owner/name.git",
                    "https://github.com/owner/name/"):
            self.assertEqual(gitcred.parse_url(url), ("github.com", "owner/name"))

    def test_ssh_and_local_transports_are_refused(self):
        for bad in ("git@github.com:owner/name.git", "ssh://github.com/o/n",
                    "file:///etc", "ext::sh -c id", "/tmp/repo", ""):
            with self.assertRaises(gitcred.GitCredError):
                gitcred.parse_url(bad)

    def test_a_url_carrying_credentials_is_refused(self):
        # Precisely what this module exists to avoid: it would persist in
        # .git/config, which every agent in the workspace can read.
        with self.assertRaises(gitcred.GitCredError) as cm:
            gitcred.parse_url("https://user:token@github.com/owner/name")
        self.assertIn("must not contain credentials", str(cm.exception))

    def test_a_host_off_the_allowlist_is_refused(self):
        with self.assertRaises(gitcred.GitCredError):
            gitcred.parse_url("https://evil.example/owner/name")

    def test_dot_dot_and_odd_paths_are_refused(self):
        for bad in ("https://github.com/owner/../../etc",
                    "https://github.com/owner",
                    "https://github.com/"):
            with self.assertRaises(gitcred.GitCredError):
                gitcred.parse_url(bad)


class Store(GitCredBase):
    def add(self, match="owner/name", scope="read", token=TOKEN, host="github.com"):
        return gitcred.add_token(self.alice, host, match, token, scope=scope)

    def test_the_token_is_stored_0600_outside_the_workspace(self):
        entry = self.add()
        path = gitcred.cred_dir(self.alice) / f"{entry['id']}.token"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(gitcred.cred_dir(self.alice).stat().st_mode & 0o777, 0o700)
        self.assertNotIn(str(config.WORKSPACES_DIR), str(path))

    def test_the_metadata_never_holds_the_value(self):
        self.add()
        blob = (gitcred.cred_dir(self.alice) / "index.json").read_text()
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(TOKEN, json.dumps(gitcred.list_credentials(self.alice)))

    def test_listing_reports_shape_not_secret(self):
        self.add()
        [row] = gitcred.list_credentials(self.alice)
        self.assertEqual((row["host"], row["match"], row["scope"]),
                         ("github.com", "owner/name", "read"))
        self.assertIn("fine-grained", row["token_kind"])

    def test_re_registering_replaces_and_leaves_no_old_file(self):
        first = self.add()
        second = self.add(token="github_pat_22" + "y" * 40)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(gitcred.list_credentials(self.alice)), 1)
        self.assertFalse(
            (gitcred.cred_dir(self.alice) / f"{first['id']}.token").exists())

    def test_read_and_write_coexist(self):
        self.add(scope="read")
        self.add(scope="write")
        self.assertEqual(len(gitcred.list_credentials(self.alice)), 2)

    def test_remove(self):
        entry = self.add()
        self.assertTrue(gitcred.remove(self.alice, entry["id"]))
        self.assertEqual(gitcred.list_credentials(self.alice), [])
        self.assertFalse(
            (gitcred.cred_dir(self.alice) / f"{entry['id']}.token").exists())
        self.assertFalse(gitcred.remove(self.alice, entry["id"]))


class Selection(GitCredBase):
    def test_no_credential_means_anonymous(self):
        self.assertIsNone(
            gitcred.select(self.alice, "github.com", "owner/name"))

    def test_an_exact_match_beats_a_wildcard(self):
        gitcred.add_token(self.alice, "github.com", "owner/*", TOKEN)
        exact = gitcred.add_token(self.alice, "github.com", "owner/name", TOKEN)
        chosen = gitcred.select(self.alice, "github.com", "owner/name")
        self.assertEqual(chosen["id"], exact["id"])

    def test_a_wildcard_covers_the_owners_repos(self):
        gitcred.add_token(self.alice, "github.com", "owner/*", TOKEN)
        self.assertIsNotNone(
            gitcred.select(self.alice, "github.com", "owner/other"))
        self.assertIsNone(
            gitcred.select(self.alice, "github.com", "someone/else"))

    def test_a_credential_is_never_offered_to_another_host(self):
        gitcred.add_token(self.alice, "github.com", "owner/name", TOKEN)
        self.assertIsNone(
            gitcred.select(self.alice, "gitlab.com", "owner/name"))

    def test_a_write_credential_is_not_returned_for_a_read_request(self):
        # A caller that only needs to read must not silently gain the ability
        # to push.
        gitcred.add_token(self.alice, "github.com", "owner/name", TOKEN,
                          scope="write")
        self.assertIsNone(
            gitcred.select(self.alice, "github.com", "owner/name", scope="read"))
        self.assertIsNotNone(
            gitcred.select(self.alice, "github.com", "owner/name", scope="write"))


class Environment(GitCredBase):
    def setUp(self):
        super().setUp()
        self.entry = gitcred.add_token(
            self.alice, "github.com", "owner/name", TOKEN)

    def test_the_token_travels_only_in_a_config_value(self):
        env = gitcred.git_env(self.alice, self.entry)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"],
                         "http.https://github.com/.extraHeader")
        expected = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        self.assertEqual(env["GIT_CONFIG_VALUE_0"],
                         f"Authorization: Basic {expected}")
        # Nowhere else in the environment.
        for key, value in env.items():
            if key != "GIT_CONFIG_VALUE_0":
                self.assertNotIn(TOKEN, value, key)

    def test_the_header_is_scoped_to_one_host(self):
        # Without the host in the key, git would offer the header to any
        # redirect target.
        env = gitcred.git_env(self.alice, self.entry)
        self.assertIn("github.com", env["GIT_CONFIG_KEY_0"])

    def test_gitlab_uses_its_own_basic_auth_user(self):
        entry = gitcred.add_token(self.alice, "gitlab.com", "owner/name",
                                  "glpat-" + "x" * 24)
        env = gitcred.git_env(self.alice, entry)
        decoded = base64.b64decode(
            env["GIT_CONFIG_VALUE_0"].split()[-1]).decode()
        self.assertTrue(decoded.startswith("oauth2:"))

    def test_prompts_are_disabled_so_a_failure_is_not_a_hang(self):
        env = gitcred.git_env(self.alice, self.entry)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "/bin/true")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_anonymous_access_carries_no_credential_keys(self):
        env = gitcred.git_env(self.alice, None)
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertNotIn(TOKEN, json.dumps(env))

    def test_what_may_be_logged_excludes_the_value(self):
        fields = gitcred.safe_log_fields(self.entry)
        self.assertEqual(fields["credential"], self.entry["id"])
        self.assertNotIn(TOKEN, json.dumps(fields))
        self.assertEqual(gitcred.safe_log_fields(None)["credential"],
                         "anonymous")


class OriginUrl(GitCredBase):
    """Read as text, because `git config` would interpret an agent's repo."""

    def repo(self, body: str) -> Path:
        repo = Path(self._tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "config").write_text(body)
        return repo

    def test_it_finds_the_origin_url(self):
        repo = self.repo('[core]\n\tbare = false\n'
                         '[remote "origin"]\n'
                         '\turl = https://github.com/owner/name.git\n'
                         '\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
        self.assertEqual(gitcred.origin_url(repo),
                         "https://github.com/owner/name.git")

    def test_another_remote_is_not_mistaken_for_origin(self):
        repo = self.repo('[remote "upstream"]\n'
                         '\turl = https://github.com/someone/else.git\n'
                         '[remote "origin"]\n'
                         '\turl = https://github.com/owner/name.git\n')
        self.assertEqual(gitcred.origin_url(repo),
                         "https://github.com/owner/name.git")

    def test_a_repo_without_origin_is_a_caller_error(self):
        with self.assertRaises(gitcred.GitCredError):
            gitcred.origin_url(self.repo("[core]\n\tbare = false\n"))

    def test_a_directory_that_is_not_a_repo(self):
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        with self.assertRaises(gitcred.GitCredError):
            gitcred.origin_url(plain)

    def test_an_include_directive_is_not_followed(self):
        # `git config` would read the included file. This must not: an agent
        # writes this config, and pointing it at a file it cannot otherwise
        # read should achieve nothing whatsoever.
        repo = self.repo('[include]\n\tpath = /etc/shadow\n'
                         '[remote "origin"]\n'
                         '\turl = https://github.com/owner/name.git\n')
        self.assertEqual(gitcred.origin_url(repo),
                         "https://github.com/owner/name.git")


class Mirrors(GitCredBase):
    def test_the_mirror_lives_outside_the_workspace(self):
        path = gitcred.mirror_path(self.alice, "github.com", "owner/name")
        self.assertNotIn(str(config.WORKSPACES_DIR), str(path))
        self.assertTrue(str(path).endswith("github.com/owner/name.git"))

    def test_each_tenant_has_its_own(self):
        (config.TENANTS_DIR / "bob.json").write_text(json.dumps({
            "vhost": "ab-bob", "bus_user": "ab-bob", "bus_password": "p",
            "limits": {"max_workers": 1}}))
        bob = tenant_from_headers({config.USER_HEADER: "bob"})
        self.assertNotEqual(
            gitcred.mirror_path(self.alice, "github.com", "owner/name"),
            gitcred.mirror_path(bob, "github.com", "owner/name"))


class FetchLeaf(GitCredBase):
    """The container half of fetch_repo (I-3)."""

    def argv(self):
        from agentbridge.service import checkpoint
        return checkpoint.fetch_command(
            self.alice, "implementer", "proj",
            str(gitcred.mirror_path(self.alice, "github.com", "owner/name")))

    def test_no_network_no_credential_no_claude(self):
        joined = " ".join(self.argv())
        self.assertIn("--network=none", joined)
        self.assertNotIn("--secret", joined)
        self.assertNotIn("claude", joined)
        self.assertNotIn("GIT_CONFIG_VALUE", joined)

    def test_the_mirror_is_mounted_read_only(self):
        argv = self.argv()
        mount = next(a for a in argv if a.endswith(":/mirror:ro"))
        self.assertIn(".mirrors", mount)

    def test_the_workspace_is_writable_and_the_script_is_not(self):
        argv = self.argv()
        self.assertIn(f"{self.alice.workspace}:/workspace:rw", argv)
        script = next(a for a in argv if a.endswith("/script.sh:ro"))
        self.assertFalse(script.startswith(str(self.alice.workspace)))


if __name__ == "__main__":
    unittest.main()


class TokenWhitespace(GitCredBase):
    """A pasted token often carries whitespace; it must not look like a typo."""

    def test_surrounding_whitespace_is_accepted_and_not_stored(self):
        entry = gitcred.add_token(
            self.alice, "github.com", "o/n", f"  {TOKEN}\n")
        self.assertEqual(entry["token_kind"], "github fine-grained")
        env = gitcred.git_env(
            self.alice, gitcred.select(self.alice, "github.com", "o/n"))
        blob = base64.b64decode(
            env["GIT_CONFIG_VALUE_0"].split("Basic ")[1]).decode()
        self.assertEqual(blob, f"x-access-token:{TOKEN}")

    def test_a_leading_space_does_not_become_an_unrecognised_prefix(self):
        try:
            gitcred.add_token(self.alice, "github.com", "o/n", f" {TOKEN}")
        except gitcred.GitCredError as exc:
            self.fail(f"a leading space was rejected as a bad token: {exc}")


class Fingerprint(GitCredBase):
    """Which token is stored has to be answerable, or a stale one is invisible."""

    def test_registration_records_a_fingerprint_matching_sha256(self):
        entry = gitcred.add_token(self.alice, "github.com", "o/n", TOKEN)
        expected = hashlib.sha256(TOKEN.encode()).hexdigest()[:6]
        self.assertEqual(entry["fingerprint"], expected)
        self.assertEqual(
            gitcred.list_credentials(self.alice)[0]["fingerprint"], expected)

    def test_whitespace_does_not_change_the_fingerprint(self):
        # The operator compares against `printf %s "$TOKEN" | sha256sum`, which
        # sees no whitespace, so a pasted newline must not shift the answer.
        self.assertEqual(gitcred.token_fingerprint(f"  {TOKEN}\n"),
                         gitcred.token_fingerprint(TOKEN))

    def test_two_tokens_are_distinguishable(self):
        other = TOKEN[:-1] + "9"
        self.assertNotEqual(gitcred.token_fingerprint(TOKEN),
                            gitcred.token_fingerprint(other))

    def test_the_fingerprint_is_not_the_token(self):
        entry = gitcred.add_token(self.alice, "github.com", "o/n", TOKEN)
        self.assertNotIn(entry["fingerprint"], TOKEN)
        self.assertEqual(len(entry["fingerprint"]), 6)

    def test_backfill_fills_an_entry_registered_without_one(self):
        gitcred.add_token(self.alice, "github.com", "o/n", TOKEN)
        # Simulate an index written before fingerprints existed.
        path = gitcred.cred_dir(self.alice) / "index.json"
        entries = json.loads(path.read_text())
        del entries[0]["fingerprint"]
        path.write_text(json.dumps(entries))
        self.assertIsNone(
            gitcred.list_credentials(self.alice)[0].get("fingerprint"))

        self.assertEqual(gitcred.backfill_fingerprints(self.alice), 1)
        self.assertEqual(gitcred.list_credentials(self.alice)[0]["fingerprint"],
                         hashlib.sha256(TOKEN.encode()).hexdigest()[:6])
        # Idempotent: nothing left to fill.
        self.assertEqual(gitcred.backfill_fingerprints(self.alice), 0)

    def test_backfill_survives_an_unreadable_token_file(self):
        gitcred.add_token(self.alice, "github.com", "o/n", TOKEN)
        path = gitcred.cred_dir(self.alice) / "index.json"
        entries = json.loads(path.read_text())
        del entries[0]["fingerprint"]
        path.write_text(json.dumps(entries))
        (gitcred.cred_dir(self.alice) / f"{entries[0]['id']}.token").unlink()
        # A listing must not become a failure because a token file is gone.
        self.assertEqual(gitcred.backfill_fingerprints(self.alice), 0)
        self.assertEqual(len(gitcred.list_credentials(self.alice)), 1)


class HttpDiagnosis(unittest.TestCase):
    """401 and 403 are different faults; GitHub's wording hides the difference."""

    def test_403_says_the_credential_was_accepted_then_refused(self):
        text = gitcred.diagnose_git_http_error(
            "remote: Write access to repository not granted.\n"
            "fatal: unable to access 'https://github.com/o/n.git/': "
            "The requested URL returned error: 403")
        self.assertIn("accepted this credential", text)
        self.assertIn("Contents: Read-only", text)

    def test_401_points_at_the_token_not_its_grants(self):
        text = gitcred.diagnose_git_http_error(
            "The requested URL returned error: 401")
        self.assertIn("did not accept", text)
        self.assertIn("fingerprint", text)

    def test_404_reads_as_invisible_rather_than_forbidden(self):
        self.assertIn("not visible", gitcred.diagnose_git_http_error(
            "The requested URL returned error: 404"))

    def test_a_prompt_fallback_is_reported_as_a_rejected_credential(self):
        # git asks for a username after a 401 and then fails for want of a
        # terminal; that is still a rejected credential.
        for text in ("fatal: could not read Username for "
                     "'https://github.com': terminal prompts disabled",
                     "remote: Authentication failed for 'https://github.com/'"):
            self.assertEqual(gitcred.diagnose_git_http_error(text),
                             gitcred.HTTP_DIAGNOSIS[401])

    def test_an_unrelated_failure_gets_no_diagnosis(self):
        for text in ("", "fatal: not a git repository",
                     "The requested URL returned error: 500"):
            self.assertIsNone(gitcred.diagnose_git_http_error(text))
