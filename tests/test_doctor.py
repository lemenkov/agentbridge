# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""`agentbridge-admin doctor`: the checks, and the promises they make.

Two promises are load-bearing and are tested as such: a check that could not
run reports `skip` rather than `ok`, and no check ever starts a container.
A diagnostic that reports health it did not measure, or that costs money to
run, is worse than no diagnostic.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentbridge.service import config, doctor
from agentbridge.service.roles import Role


def probe_returning(mapping):
    """A probe that answers from a dict, and records what it was asked."""
    asked = []

    def probe(url):
        asked.append(url)
        if url not in mapping:
            return None, b"", "connection refused"
        status, body = mapping[url]
        return status, body, ""

    probe.asked = asked
    return probe


def tags(*names):
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


OLLAMA = """
[local]
kind = "ollama"
endpoints = [{ url = "http://127.0.0.1:11434" }]
models = ["llama3.2:3b"]
"""


class TempFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, name, text, mode=0o644):
        path = self.tmp / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def levels(self, findings, check_prefix=""):
        return [f.level for f in findings if f.check.startswith(check_prefix)]


class Backends(TempFiles):
    def test_a_file_that_does_not_load_is_a_failure_naming_the_reason(self):
        path = self.write("b.toml", '[local]\nkind = "ollama"\n')
        [finding] = doctor.check_backends(path)
        self.assertEqual(finding.level, doctor.FAIL)
        self.assertIn("needs `endpoints`", finding.detail)

    def test_a_good_file_passes_and_names_its_backends(self):
        path = self.write("b.toml", OLLAMA)
        findings = doctor.check_backends(path)
        self.assertEqual(self.levels(findings, "backends.toml ")[0], doctor.WARN)
        self.assertEqual(findings[0].level, doctor.OK)
        self.assertIn("anthropic", findings[0].detail)
        self.assertIn("local", findings[0].detail)

    def test_a_world_writable_file_fails(self):
        # It decides where every leaf sends its requests.
        path = self.write("b.toml", OLLAMA, mode=0o666)
        findings = doctor.check_backends(path)
        mode_findings = [f for f in findings if f.check == "backends.toml mode"]
        self.assertEqual([f.level for f in mode_findings], [doctor.FAIL])

    def test_an_absent_file_is_the_documented_default_not_an_error(self):
        [finding] = doctor.check_backends(self.tmp / "nope.toml")
        self.assertEqual(finding.level, doctor.OK)
        self.assertIn("hosted Anthropic API only", finding.detail)


class Roles(TempFiles):
    def setUp(self):
        super().setUp()
        (self.tmp / "SKILLS").mkdir()
        self.role = Role(skill="researcher", image="img", workspace="ro",
                         description="d")
        self._patches = [
            mock.patch.object(config, "AGENTBRIDGE_HOME", self.tmp),
            mock.patch.dict(doctor.ROLES, {"researcher": self.role}, clear=True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_missing_preamble_fails(self):
        (self.tmp / "AGENT.md").write_text("x")
        findings = doctor.check_roles(self.tmp / "nope.toml")
        self.assertEqual(findings[0].level, doctor.FAIL)
        self.assertIn("preamble missing", findings[0].detail)

    def test_an_empty_preamble_fails_too(self):
        # An empty file is a worker running with no instructions, which is
        # worse than one that fails to start.
        (self.tmp / "AGENT.md").write_text("x")
        (self.tmp / "SKILLS" / "researcher.md").write_text("")
        findings = doctor.check_roles(self.tmp / "nope.toml")
        self.assertEqual(findings[0].level, doctor.FAIL)
        self.assertIn("empty", findings[0].detail)

    def test_an_unloadable_registry_does_not_hide_the_role_findings(self):
        # Regression: check_roles called load() unguarded, so a bad
        # backends.toml raised out of run_all and suppressed every other check.
        (self.tmp / "AGENT.md").write_text("x")
        (self.tmp / "SKILLS" / "researcher.md").write_text("y")
        bad = self.write("bad.toml", '[local]\nkind = "ollama"\n')
        findings = doctor.check_roles(bad)
        self.assertEqual([f.level for f in findings], [doctor.OK])

    def test_a_role_pointing_at_an_unknown_backend_fails(self):
        (self.tmp / "AGENT.md").write_text("x")
        (self.tmp / "SKILLS" / "researcher.md").write_text("y")
        with mock.patch.dict(
                doctor.ROLES,
                {"researcher": Role(skill="researcher", image="img",
                                    workspace="ro", description="d",
                                    default_backend="nowhere")}, clear=True):
            findings = doctor.check_roles(self.tmp / "nope.toml")
        self.assertIn(doctor.FAIL, [f.level for f in findings])


class Endpoints(TempFiles):
    def test_an_unreachable_endpoint_fails(self):
        path = self.write("b.toml", OLLAMA)
        findings = doctor.check_endpoints(probe_returning({}), path)
        local = [f for f in findings if f.check == "backend local"]
        self.assertEqual([f.level for f in local], [doctor.FAIL])
        self.assertIn("unreachable", local[0].detail)

    def test_any_http_status_counts_as_reachable(self):
        # A 401 from a model server still proves it is listening, which is all
        # this check claims to measure.
        path = self.write("b.toml", OLLAMA)
        probe = probe_returning({"http://127.0.0.1:11434/api/tags": (401, b"")})
        findings = doctor.check_endpoints(probe, path)
        local = [f for f in findings if f.check == "backend local"]
        self.assertEqual([f.level for f in local], [doctor.OK])

    def test_the_hosted_api_is_skipped_not_probed(self):
        path = self.write("b.toml", OLLAMA)
        probe = probe_returning({"http://127.0.0.1:11434/api/tags": (200, b"{}")})
        findings = doctor.check_endpoints(probe, path)
        hosted = [f for f in findings if f.check == "backend anthropic"]
        self.assertEqual([f.level for f in hosted], [doctor.SKIP])
        self.assertNotIn("https://api.anthropic.com", "".join(probe.asked))


class Models(TempFiles):
    def setUp(self):
        super().setUp()
        self.path = self.write("b.toml", OLLAMA)
        self.url = "http://127.0.0.1:11434/api/tags"

    def test_an_allowed_but_unpulled_model_fails_and_says_what_is_there(self):
        probe = probe_returning({self.url: (200, tags("mistral:7b"))})
        [finding] = doctor.check_models(probe, self.path)
        self.assertEqual(finding.level, doctor.FAIL)
        self.assertIn("llama3.2:3b", finding.detail)
        self.assertIn("mistral:7b", finding.detail)

    def test_a_pulled_model_passes(self):
        probe = probe_returning({self.url: (200, tags("llama3.2:3b"))})
        [finding] = doctor.check_models(probe, self.path)
        self.assertEqual(finding.level, doctor.OK)

    def test_a_bare_name_matches_the_latest_tag(self):
        bare = self.write("bare.toml", OLLAMA.replace('"llama3.2:3b"', '"mistral"'))
        probe = probe_returning({self.url: (200, tags("mistral:latest"))})
        [finding] = doctor.check_models(probe, bare)
        self.assertEqual(finding.level, doctor.OK)

    def test_an_unreachable_endpoint_is_skipped_not_passed(self):
        # The distinction this module exists to keep: not measured is not well.
        [finding] = doctor.check_models(probe_returning({}), self.path)
        self.assertEqual(finding.level, doctor.SKIP)

    def test_junk_instead_of_json_fails(self):
        probe = probe_returning({self.url: (200, b"<html>nope")})
        [finding] = doctor.check_models(probe, self.path)
        self.assertEqual(finding.level, doctor.FAIL)


class Images(unittest.TestCase):
    def test_without_a_runner_the_check_is_skipped_not_passed(self):
        findings = doctor.check_images(None)
        self.assertEqual([f.level for f in findings], [doctor.SKIP])
        self.assertIn("as root", findings[0].detail)

    def test_a_missing_image_fails(self):
        def runner(*argv):
            return mock.Mock(returncode=1)
        self.assertTrue(all(f.level == doctor.FAIL
                            for f in doctor.check_images(runner)))

    def test_a_present_image_passes(self):
        def runner(*argv):
            return mock.Mock(returncode=0)
        self.assertTrue(all(f.level == doctor.OK
                            for f in doctor.check_images(runner)))

    def test_it_asks_only_whether_the_image_exists(self):
        # `podman image exists` inspects local storage. `podman run` would make
        # a diagnostic that costs money and changes state.
        calls = []

        def runner(*argv):
            calls.append(list(argv))
            return mock.Mock(returncode=0)

        doctor.check_images(runner)
        self.assertTrue(calls)
        for argv in calls:
            self.assertEqual(argv[:3], ["podman", "image", "exists"])
            self.assertNotIn("run", argv)


class ExitCode(unittest.TestCase):
    def test_only_a_failure_is_a_bad_exit(self):
        f = doctor.Finding
        self.assertEqual(doctor.exit_code([f("a", doctor.OK, "")]), 0)
        self.assertEqual(doctor.exit_code([f("a", doctor.WARN, "")]), 0)
        self.assertEqual(doctor.exit_code([f("a", doctor.SKIP, "")]), 0)
        self.assertEqual(doctor.exit_code([f("a", doctor.FAIL, "")]), 1)

    def test_worst_orders_fail_over_warn_over_skip(self):
        f = doctor.Finding
        self.assertEqual(doctor.worst([f("a", doctor.OK, ""),
                                       f("b", doctor.SKIP, "")]), doctor.SKIP)
        self.assertEqual(doctor.worst([f("a", doctor.WARN, ""),
                                       f("b", doctor.SKIP, "")]), doctor.WARN)
        self.assertEqual(doctor.worst([f("a", doctor.WARN, ""),
                                       f("b", doctor.FAIL, "")]), doctor.FAIL)
        self.assertEqual(doctor.worst([f("a", doctor.OK, "")]), doctor.OK)


class RunAllSpawnsNothing(TempFiles):
    def test_no_check_ever_starts_a_container(self):
        calls = []

        def runner(*argv):
            calls.append(list(argv))
            return mock.Mock(returncode=0)

        path = self.write("b.toml", OLLAMA)
        probe = probe_returning(
            {"http://127.0.0.1:11434/api/tags": (200, tags("llama3.2:3b"))})
        findings = doctor.run_all(probe=probe, runner=runner, path=path)
        self.assertTrue(findings)
        self.assertTrue(calls)
        for argv in calls:
            self.assertNotIn("run", argv)
            self.assertNotIn("create", argv)
            self.assertNotIn("start", argv)
        # And it asked nothing of the hosted API, which would cost a token.
        self.assertTrue(all("anthropic.com" not in u for u in probe.asked))


if __name__ == "__main__":
    unittest.main()
