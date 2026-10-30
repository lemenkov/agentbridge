# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The backend registry.

Most of this is about refusing a file rather than accepting it: a backend that
loads but is wrong produces workers that spawn and then fail every task, which
is harder to diagnose than a service that will not start and says why.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from agentbridge.service import backends

EXAMPLE = textwrap.dedent("""
    [anthropic]
    kind = "anthropic"

    [ollama]
    kind = "ollama"
    models = ["qwen3-coder", "gpt-oss:20b"]
    default_model = "qwen3-coder"
    task_timeout = 900
    endpoints = [
        { url = "http://mini.lan:11434",  max_workers = 2 },
        { url = "http://10.8.0.5:11434",  max_workers = 1 },
    ]

    [gateway-x]
    kind = "anthropic-compatible"
    models = ["some-model"]
    credential = "backend-gateway-x-token"
    tenants = ["claude"]
    endpoints = [ { url = "https://api.example.net" } ]
    """)


class BackendsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def load(self, toml: str):
        path = self.dir / "backends.toml"
        path.write_text(toml, encoding="utf-8")
        return backends.load(path)

    def refuses(self, toml: str, *fragments: str):
        with self.assertRaises(backends.BackendError) as caught:
            self.load(toml)
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message


class NoFile(BackendsBase):
    """A host with no backends.toml must work, and get the hosted API."""

    def test_a_missing_file_yields_anthropic_only(self):
        registry = backends.load(self.dir / "absent.toml")
        self.assertEqual(list(registry), ["anthropic"])
        self.assertEqual(registry["anthropic"].kind, "anthropic")

    def test_an_empty_file_yields_anthropic_only(self):
        self.assertEqual(list(self.load("")), ["anthropic"])

    def test_anthropic_is_added_even_when_the_file_omits_it(self):
        registry = self.load(textwrap.dedent("""
            [ollama]
            kind = "ollama"
            models = ["m"]
            endpoints = [ { url = "http://10.0.0.2:11434" } ]
            """))
        self.assertIn("anthropic", registry)

    def test_a_directory_is_an_error_not_a_silent_default(self):
        with self.assertRaises(backends.BackendError):
            backends.load(self.dir)

    def test_invalid_toml_names_the_file(self):
        self.refuses("[oops\nkind =", "not valid TOML")


class Example(BackendsBase):
    """The documented example in PLAN.md E.2 has to load as written."""

    def test_it_loads(self):
        registry = self.load(EXAMPLE)
        self.assertEqual(sorted(registry), ["anthropic", "gateway-x", "ollama"])

    def test_endpoints_keep_order_caps_and_hosts(self):
        ollama = self.load(EXAMPLE)["ollama"]
        self.assertEqual([(e.url, e.max_workers) for e in ollama.endpoints],
                         [("http://mini.lan:11434", 2),
                          ("http://10.8.0.5:11434", 1)])
        self.assertEqual(ollama.endpoints[0].host, "mini.lan")

    def test_task_timeout_and_default_model_survive(self):
        ollama = self.load(EXAMPLE)["ollama"]
        self.assertEqual(ollama.task_timeout, 900)
        self.assertEqual(ollama.default_model, "qwen3-coder")


class TenantScope(BackendsBase):
    def test_an_empty_tenant_list_means_everyone(self):
        self.assertTrue(self.load(EXAMPLE)["ollama"].allows("anyone"))

    def test_a_tenant_list_excludes_everyone_else(self):
        gateway = self.load(EXAMPLE)["gateway-x"]
        self.assertTrue(gateway.allows("claude"))
        self.assertFalse(gateway.allows("bob"))


class ModelChoice(BackendsBase):
    def test_the_first_model_becomes_the_default_when_unset(self):
        registry = self.load(textwrap.dedent("""
            [ollama]
            kind = "ollama"
            models = ["first", "second"]
            endpoints = [ { url = "http://10.0.0.2:11434" } ]
            """))
        self.assertEqual(registry["ollama"].default_model, "first")

    def test_a_default_outside_models_is_refused(self):
        self.refuses(textwrap.dedent("""
            [ollama]
            kind = "ollama"
            models = ["a"]
            default_model = "b"
            endpoints = [ { url = "http://10.0.0.2:11434" } ]
            """), "default_model", "not in")

    def test_model_or_default_falls_back_then_honours_a_request(self):
        ollama = self.load(EXAMPLE)["ollama"]
        self.assertEqual(ollama.model_or_default(None), "qwen3-coder")
        self.assertEqual(ollama.model_or_default("gpt-oss:20b"), "gpt-oss:20b")

    def test_anthropic_validates_its_own_model_names(self):
        # Returning None keeps this module out of the business of tracking
        # Anthropic's model ids in a second place.
        self.assertIsNone(backends.ANTHROPIC.model_or_default(None))
        self.assertEqual(backends.ANTHROPIC.model_or_default("opus"), "opus")


class Kinds(BackendsBase):
    def test_an_unknown_kind_is_refused(self):
        self.refuses('[x]\nkind = "magic"\n', "kind must be one of")

    def test_a_missing_kind_is_refused(self):
        self.refuses('[x]\nmodels = ["m"]\n', "kind must be one of")

    def test_an_unknown_key_is_refused_rather_than_ignored(self):
        # A typo that is ignored is a setting the operator thinks is in force.
        self.refuses('[x]\nkind = "anthropic"\nmodls = ["m"]\n',
                     "unknown key", "modls")

    def test_an_unknown_endpoint_key_is_refused(self):
        self.refuses(textwrap.dedent("""
            [ollama]
            kind = "ollama"
            models = ["m"]
            endpoints = [ { url = "http://10.0.0.2:11434", workers = 2 } ]
            """), "unknown endpoint key", "workers")

    def test_anthropic_takes_no_endpoints_or_models(self):
        self.refuses('[anthropic]\nkind = "anthropic"\n'
                     'endpoints = [ { url = "https://a.example" } ]\n',
                     "takes no `endpoints`")
        self.refuses('[anthropic]\nkind = "anthropic"\nmodels = ["opus"]\n',
                     "takes no `models`")

    def test_ollama_needs_endpoints_and_models(self):
        self.refuses('[o]\nkind = "ollama"\nmodels = ["m"]\n', "needs `endpoints`")
        self.refuses('[o]\nkind = "ollama"\n'
                     'endpoints = [ { url = "http://10.0.0.2:11434" } ]\n',
                     "needs `models`")

    def test_a_credential_is_only_for_anthropic_compatible(self):
        self.refuses(textwrap.dedent("""
            [o]
            kind = "ollama"
            models = ["m"]
            credential = "some-secret"
            endpoints = [ { url = "http://10.0.0.2:11434" } ]
            """), "only for", "anthropic-compatible")

    def test_anthropic_compatible_needs_a_credential(self):
        self.refuses(textwrap.dedent("""
            [g]
            kind = "anthropic-compatible"
            models = ["m"]
            endpoints = [ { url = "https://api.example.net" } ]
            """), "needs a `credential`")


class UrlShape(BackendsBase):
    """An endpoint is scheme, host and port. Anything else is refused."""

    def _ollama(self, url: str) -> str:
        return textwrap.dedent(f"""
            [o]
            kind = "ollama"
            models = ["m"]
            endpoints = [ {{ url = "{url}" }} ]
            """)

    def test_a_path_is_refused_because_callers_append_their_own(self):
        self.refuses(self._ollama("http://10.0.0.2:11434/api"), "must have no path")

    def test_a_bare_trailing_slash_is_accepted_and_normalised(self):
        registry = self.load(self._ollama("http://10.0.0.2:11434/"))
        self.assertEqual(registry["o"].endpoints[0].url, "http://10.0.0.2:11434")

    def test_a_query_or_fragment_is_refused(self):
        self.refuses(self._ollama("http://10.0.0.2:11434?a=b"),
                     "no query or fragment")

    def test_userinfo_is_refused_because_the_file_is_world_readable(self):
        message = self.refuses(self._ollama("http://user:pw@10.0.0.2:11434"),
                               "userinfo")
        self.assertIn("world-readable", message)

    def test_a_non_http_scheme_is_refused(self):
        self.refuses(self._ollama("ssh://10.0.0.2:22"), "must be http:// or https://")

    def test_a_missing_url_is_refused(self):
        self.refuses(textwrap.dedent("""
            [o]
            kind = "ollama"
            models = ["m"]
            endpoints = [ { max_workers = 1 } ]
            """), "needs a url")

    def test_a_duplicate_endpoint_is_refused(self):
        self.refuses(textwrap.dedent("""
            [o]
            kind = "ollama"
            models = ["m"]
            endpoints = [ { url = "http://10.0.0.2:11434" },
                          { url = "http://10.0.0.2:11434" } ]
            """), "listed twice")

    def test_max_workers_must_be_a_positive_integer(self):
        for bad in ("0", "-1", '"two"', "true"):
            # An inline TOML table has to stay on one line.
            self.refuses(textwrap.dedent(f"""
                [o]
                kind = "ollama"
                models = ["m"]
                endpoints = [ {{ url = "http://10.0.0.2:11434", max_workers = {bad} }} ]
                """), "max_workers")

    def test_task_timeout_must_be_a_positive_integer(self):
        for bad in ("0", "-5", "true"):
            self.refuses(textwrap.dedent(f"""
                [o]
                kind = "ollama"
                models = ["m"]
                task_timeout = {bad}
                endpoints = [ {{ url = "http://10.0.0.2:11434" }} ]
                """), "task_timeout")


class Plaintext(BackendsBase):
    """http:// is for the LAN. A public address, or a token, needs TLS."""

    def test_plaintext_to_a_private_address_is_allowed(self):
        for host in ("10.8.0.5", "192.168.1.9", "172.16.0.3", "127.0.0.1"):
            registry = self.load(textwrap.dedent(f"""
                [o]
                kind = "ollama"
                models = ["m"]
                endpoints = [ {{ url = "http://{host}:11434" }} ]
                """))
            self.assertEqual(len(registry["o"].endpoints), 1, host)

    def test_plaintext_to_a_public_address_is_refused(self):
        self.refuses(textwrap.dedent("""
            [o]
            kind = "ollama"
            models = ["m"]
            endpoints = [ { url = "http://8.8.8.8:11434" } ]
            """), "plaintext to a public address")

    def test_a_hostname_is_not_resolved_at_load_time(self):
        # Resolving here would make the service refuse to start whenever DNS
        # is briefly down, and the answer could change later anyway.
        registry = self.load(textwrap.dedent("""
            [o]
            kind = "ollama"
            models = ["m"]
            endpoints = [ { url = "http://mini.lan:11434" } ]
            """))
        self.assertEqual(registry["o"].endpoints[0].host, "mini.lan")

    def test_a_credential_over_plaintext_to_a_hostname_is_refused(self):
        # The one case where an unresolvable name is not given the benefit of
        # the doubt: a token is about to be sent somewhere.
        self.refuses(textwrap.dedent("""
            [g]
            kind = "anthropic-compatible"
            models = ["m"]
            credential = "tok"
            endpoints = [ { url = "http://gateway.example:8080" } ]
            """), "plaintext", "credential")

    def test_https_is_always_allowed(self):
        registry = self.load(textwrap.dedent("""
            [g]
            kind = "anthropic-compatible"
            models = ["m"]
            credential = "tok"
            endpoints = [ { url = "https://api.example.net" } ]
            """))
        self.assertEqual(registry["g"].endpoints[0].url, "https://api.example.net")


class Accessors(BackendsBase):
    def test_get_names_what_the_host_has_when_asked_for_something_else(self):
        backends.backends(reload=True)
        with self.assertRaises(backends.BackendError) as caught:
            backends.get("nope")
        self.assertIn("unknown backend", str(caught.exception))
        self.assertIn("anthropic", str(caught.exception))

    def test_for_tenant_hides_what_a_tenant_may_not_use(self):
        import unittest.mock as mock
        registry = self.load(EXAMPLE)
        with mock.patch.object(backends, "_cache", registry):
            self.assertIn("gateway-x", backends.for_tenant("claude"))
            self.assertNotIn("gateway-x", backends.for_tenant("bob"))
            self.assertIn("ollama", backends.for_tenant("bob"))
            self.assertIn("anthropic", backends.for_tenant("bob"))


class ShippedExample(unittest.TestCase):
    """deploy/backends.toml.example has to load, commented and uncommented.

    A config example nobody has parsed is the same defect as a command nobody
    has run: it looks like documentation and is actually a guess.
    """

    PATH = Path(__file__).resolve().parent.parent / "deploy" / "backends.toml.example"

    def test_it_exists(self):
        self.assertTrue(self.PATH.is_file(), self.PATH)

    def test_as_shipped_it_loads_and_yields_anthropic_only(self):
        self.assertEqual(list(backends.load(self.PATH)), ["anthropic"])

    def test_with_every_section_uncommented_it_still_loads(self):
        import re
        lines = []
        for line in self.PATH.read_text(encoding="utf-8").splitlines():
            # Uncomment the example backends, leaving the prose comments alone.
            if re.match(r"^#(\[|kind|models|default_model|task_timeout|"
                        r"credential|tenants|endpoints|\s+\{|\])", line):
                line = line[1:]
            lines.append(line)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backends.toml"
            path.write_text("\n".join(lines), encoding="utf-8")
            registry = backends.load(path)
        self.assertEqual(sorted(registry), ["anthropic", "gateway-x", "ollama"])
        self.assertEqual(registry["ollama"].default_model, "qwen3-coder")
        self.assertEqual(registry["gateway-x"].credential,
                         "backend-gateway-x-token")
