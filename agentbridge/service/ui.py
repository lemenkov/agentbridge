# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The live fleet page: five read-only routes on the existing app and port.

Served by the same FastMCP app via ``custom_route``, so there is no new unit,
no new port and no second thing to keep running. nginx maps
``/mcp/agentbridge/ui`` here (see deploy/nginx-location.conf), and the tenant
comes from the same ``X-Forwarded-User`` header as every tool call.

Nothing here changes state. There is no POST, so there is nothing for CSRF to
forge, and every response carries a restrictive CSP: the assets are separate
files rather than inline script precisely so that ``script-src 'self'`` can be
enforced without `unsafe-inline`.

The page shows agent output only when the viewer turns it on, and the renderer
writes every such string with ``textContent``. Agent output is untrusted
input; this is what stops a prompt injection from becoming script in the
operator's browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)

from .. import ledger
from . import fleet, tap, workers
from .tenant import TenantError, tenant_from_headers

log = logging.getLogger("mcp-agentbridge")

ASSETS = Path(__file__).parent / "assets"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # A fleet view is never useful from cache.
    "Cache-Control": "no-store",
}

HEARTBEAT_SECONDS = 15.0
SNAPSHOT_LEDGER_ROWS = 50


def _tenant(request: Request):
    """Resolve the tenant from the proxy's header, as the tools do."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    return tenant_from_headers(headers)


def _asset(name: str, media_type: str) -> Response:
    try:
        body = (ASSETS / name).read_bytes()
    except OSError:
        return PlainTextResponse("not found", status_code=404,
                                 headers=SECURITY_HEADERS)
    return Response(body, media_type=media_type, headers=SECURITY_HEADERS)


def register(mcp) -> None:
    """Attach the routes to a FastMCP instance."""

    # Both spellings: nginx redirects the slashless form to the trailing
    # slash (relative asset URLs depend on it), but a direct caller may use
    # either.
    @mcp.custom_route("/ui", methods=["GET"])
    @mcp.custom_route("/ui/", methods=["GET"])
    async def page(request: Request) -> Response:
        try:
            tenant = _tenant(request)
        except TenantError as e:
            return PlainTextResponse(str(e), status_code=403,
                                     headers=SECURITY_HEADERS)
        log.info("user=%s route=/ui status=200", tenant.name)
        return _asset("fleet.html", "text/html; charset=utf-8")

    @mcp.custom_route("/ui/app.js", methods=["GET"])
    async def app_js(request: Request) -> Response:
        return _asset("app.js", "text/javascript; charset=utf-8")

    @mcp.custom_route("/ui/app.css", methods=["GET"])
    async def app_css(request: Request) -> Response:
        return _asset("app.css", "text/css; charset=utf-8")

    @mcp.custom_route("/ui/snapshot.json", methods=["GET"])
    async def snapshot(request: Request) -> Response:
        try:
            tenant = _tenant(request)
        except TenantError as e:
            return JSONResponse({"error": str(e)}, status_code=403,
                                headers=SECURITY_HEADERS)
        units = await workers.list_units(tenant)
        payload = await fleet.snapshot(tenant, units)
        payload["recent"] = await asyncio.to_thread(
            ledger.records, tenant.ledger, SNAPSHOT_LEDGER_ROWS, "1d", None)
        log.info("user=%s route=/ui/snapshot.json status=200", tenant.name)
        return JSONResponse(payload, headers=SECURITY_HEADERS)

    @mcp.custom_route("/ui/events", methods=["GET"])
    async def event_stream(request: Request) -> Response:
        try:
            tenant = _tenant(request)
        except TenantError as e:
            return PlainTextResponse(str(e), status_code=403,
                                     headers=SECURITY_HEADERS)
        # Off by default: agent output is untrusted and the page is useful
        # without it.
        bodies = request.query_params.get("bodies") == "1"

        async def stream():
            try:
                async with tap.connection_slot(tenant):
                    units = await workers.list_units(tenant)
                    first = await fleet.snapshot(tenant, units)
                    yield _sse("snapshot", first)
                    async for event in tap.events(
                        tenant, bodies=bodies, heartbeat=HEARTBEAT_SECONDS
                    ):
                        if event is None:
                            # Keeps the connection alive through a quiet fleet.
                            yield ": ping\n\n"
                        else:
                            yield _sse(event.get("type", "output"), event)
            except tap.TapBusy as e:
                yield _sse("error", {"error": str(e)})
            except (asyncio.CancelledError, GeneratorExit):
                # The browser went away; the exclusive queue auto-deletes.
                raise
            except Exception as e:                       # noqa: BLE001
                log.info("user=%s route=/ui/events error=%s",
                         tenant.name, type(e).__name__)
                yield _sse("error", {"error": "event stream failed"})

        log.info("user=%s route=/ui/events status=200 bodies=%s",
                 tenant.name, bodies)
        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={**SECURITY_HEADERS, "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
