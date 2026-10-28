# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The page's security properties.

This page displays agent output, which is untrusted: it is whatever a cloned
repository, a web page or another person's issue tracker told an agent to say.
Two defences are tested here — the headers the responses carry, and the
absence of any inline script that the CSP would otherwise have to allow.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from agentbridge.service import config, rabbit_mgmt, ui, workers
from agentbridge.service.server import mcp

ASSETS = Path(ui.__file__).parent / "assets"


class Assets(unittest.TestCase):
    """Static checks: no inline script, no inline style, no remote origins."""

    def setUp(self):
        self.html = (ASSETS / "fleet.html").read_text()
        self.js = (ASSETS / "app.js").read_text()

    def test_no_inline_script(self):
        # <script src=...> is fine; <script>code</script> is not, because the
        # CSP has no 'unsafe-inline'.
        for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>",
                                 self.html, re.DOTALL | re.IGNORECASE):
            attrs, body = match.group(1), match.group(2)
            self.assertIn("src=", attrs, "inline <script> would need unsafe-inline")
            self.assertEqual(body.strip(), "")

    def test_no_inline_style_or_event_handlers(self):
        self.assertNotRegex(self.html, r"(?i)<style\b")
        self.assertNotRegex(self.html, r"(?i)\sstyle\s*=")
        self.assertNotRegex(self.html, r"(?i)\son(click|load|error|mouseover)\s*=")

    def test_no_remote_origins(self):
        for text in (self.html, (ASSETS / "app.css").read_text(), self.js):
            self.assertNotIn("http://", text.replace("http://www.w3.org", ""))
            self.assertNotIn("https://", text)

    def test_the_renderer_never_uses_innerhtml(self):
        # Property access, not the bare word: the file's own comments explain
        # why innerHTML is avoided, and saying so must not fail the test.
        self.assertNotRegex(self.js, r"\.(inner|outer)HTML")
        self.assertNotRegex(self.js, r"insertAdjacentHTML\s*\(")
        self.assertNotRegex(self.js, r"document\.write\s*\(")
        # eval and new Function would be blocked by the CSP anyway, but the
        # renderer should not reach for them in the first place.
        self.assertNotRegex(self.js, r"\beval\s*\(")
        self.assertNotRegex(self.js, r"new\s+Function\s*\(")

    def test_urls_are_built_with_credentials_stripped(self):
        # A page opened as https://user:pass@host/... carries the userinfo in
        # document.baseURI. fetch() refuses such a URL outright, and anything
        # that logs it echoes the password.
        self.assertIn('url.username = ""', self.js)
        self.assertIn('url.password = ""', self.js)
        # Every network call goes through the helper, none through a bare
        # relative string.
        self.assertNotRegex(self.js, r'fetch\(\s*"')
        self.assertNotRegex(self.js, r'new EventSource\(\s*(?!sameOrigin)')

    def test_the_event_log_never_records_a_whole_url(self):
        # Logging url.href would put a credential into a log people copy and
        # paste; the path is enough to diagnose with.
        self.assertNotRegex(self.js, r'noteEvent\([^)]*to:\s*url\b(?!\.pathname)')

    def test_agent_strings_go_through_textcontent(self):
        # The helper that builds every node assigns textContent, and the page
        # uses it for tool names, bodies and task ids.
        self.assertIn("node.textContent = String(text)", self.js)

    def test_a_base_tag_would_be_blocked_so_there_is_none(self):
        self.assertNotRegex(self.html, r"(?i)<base\b")
        self.assertIn("base-uri 'none'", ui.SECURITY_HEADERS[
            "Content-Security-Policy"])


class Headers(unittest.TestCase):
    def test_the_policy_is_restrictive(self):
        csp = ui.SECURITY_HEADERS["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(ui.SECURITY_HEADERS["X-Content-Type-Options"], "nosniff")
        self.assertEqual(ui.SECURITY_HEADERS["Referrer-Policy"], "no-referrer")


class Routes(unittest.IsolatedAsyncioTestCase):
    """Drive the real routes through the ASGI app, with no network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for patch in (
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "WORKSPACES_DIR", root / "ws"),
            mock.patch.object(config, "RABBITMQ_ADMIN_USER", ""),
            mock.patch.object(workers, "list_units", new=mock.AsyncMock(
                return_value=[{"id": "r-1", "unit": "agentbridge-alice-r-1.service",
                               "active": "active", "sub": "running"}])),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config.TENANTS_DIR.mkdir()
        (config.TENANTS_DIR / "alice.json").write_text(json.dumps({
            "vhost": "ab-alice", "bus_user": "ab-alice",
            "bus_password": "very-secret", "limits": {"max_workers": 2},
        }))

    async def client(self):
        app = mcp.http_app()
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://svc")

    async def get(self, path, user="alice"):
        headers = {config.USER_HEADER: user} if user else {}
        async with await self.client() as http:
            # http_app() needs its lifespan for the MCP routes; ours are
            # plain Starlette routes, so a direct request is enough.
            return await http.get(path, headers=headers)

    async def test_the_page_carries_the_headers(self):
        response = await self.get("/ui/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        for name, value in ui.SECURITY_HEADERS.items():
            self.assertEqual(response.headers.get(name), value, name)

    async def test_assets_are_served_with_the_headers_and_right_types(self):
        for path, kind in (("/ui/app.js", "javascript"), ("/ui/app.css", "css")):
            response = await self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(kind, response.headers["content-type"])
            self.assertEqual(
                response.headers["Content-Security-Policy"],
                ui.SECURITY_HEADERS["Content-Security-Policy"])

    async def test_no_identity_no_page(self):
        response = await self.get("/ui/", user=None)
        self.assertEqual(response.status_code, 403)

    async def test_an_unprovisioned_user_is_refused(self):
        response = await self.get("/ui/", user="ghost")
        self.assertEqual(response.status_code, 403)

    async def test_the_snapshot_is_metadata_only(self):
        response = await self.get("/ui/snapshot.json")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user"], "alice")
        self.assertEqual(payload["totals"]["workers"], 1)
        self.assertIn("recent", payload)
        blob = json.dumps(payload)
        self.assertNotIn("very-secret", blob)
        self.assertNotIn("amqp://", blob)

    async def test_the_snapshot_refuses_an_unknown_user(self):
        response = await self.get("/ui/snapshot.json", user="ghost")
        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.json())

    async def test_only_get_is_routed(self):
        async with await self.client() as http:
            for path in ("/ui/", "/ui/snapshot.json", "/ui/events"):
                response = await http.post(
                    path, headers={config.USER_HEADER: "alice"})
                self.assertIn(response.status_code, (404, 405), path)

    async def test_queue_depths_are_absent_without_the_management_api(self):
        with mock.patch.object(rabbit_mgmt, "queues",
                               side_effect=AssertionError("must not be called")):
            response = await self.get("/ui/snapshot.json")
        self.assertNotIn("queues", response.json())


if __name__ == "__main__":
    unittest.main()
