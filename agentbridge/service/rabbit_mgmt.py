# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""RabbitMQ provisioning over the management API, so nothing needs root.

Replaces every ``rabbitmqctl`` call: that needed the Erlang cookie, hence root
or the ``rabbitmq`` user. The management API needs only an administrator
account, which the service holds in ``RABBITMQ_ADMIN_USER``/``_PASSWORD``.

The API is loopback-only (step 0.1a), and the admin password never reaches
argv, a URL or a log line: it travels as HTTP Basic auth. Passwords for the
tenants we create are sent **pre-hashed**, so the broker never sees them
either.

Every non-2xx response raises ``RabbitError``: a broken broker is a system
fault and should be loud, not folded into a caller-facing message.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import quote

import httpx

from . import config

TIMEOUT = 10.0

_client: httpx.Client | None = None


class RabbitError(RuntimeError):
    """The management API refused, or could not be reached."""


def _esc(value: str) -> str:
    """Percent-encode a path segment; vhost names such as ``/`` need it."""
    return quote(value, safe="")


def client() -> httpx.Client:
    """The shared client. Built on first use so import never needs the env."""
    global _client
    if _client is None:
        user = config.RABBITMQ_ADMIN_USER
        password = config.RABBITMQ_ADMIN_PASSWORD
        if not user or not password:
            raise RabbitError(
                "RABBITMQ_ADMIN_USER/RABBITMQ_ADMIN_PASSWORD are not set — "
                f"add them to {config.SYSCONFIG_PATH} (see plan step 0.4)"
            )
        _client = httpx.Client(
            base_url=config.RABBITMQ_API,
            auth=httpx.BasicAuth(user, password),
            timeout=TIMEOUT,
        )
    return _client


def _request(method: str, path: str, *, json: dict | None = None,
             ok_missing: bool = False, api: httpx.Client | None = None) -> httpx.Response:
    api = api or client()
    try:
        response = api.request(method, path, json=json)
    except httpx.HTTPError as exc:
        # str(exc) carries the URL, which holds no credentials (auth is a header).
        raise RabbitError(f"management API unreachable: {exc}") from None
    if ok_missing and response.status_code == 404:
        return response
    if response.status_code >= 400:
        detail = response.text.strip()[:200]
        raise RabbitError(
            f"management API {method} {path} returned {response.status_code}"
            + (f": {detail}" if detail else "")
        )
    return response


def password_hash(password: str) -> str:
    """RabbitMQ's stored form for ``rabbit_password_hashing_sha256``:
    ``base64(salt || sha256(salt || password))`` with a 4-byte random salt.
    Sending this instead of the password keeps it out of the broker's hands."""
    salt = secrets.token_bytes(4)
    digest = hashlib.sha256(salt + password.encode()).digest()
    return base64.b64encode(salt + digest).decode()


def ensure_vhost(vhost: str, *, api: httpx.Client | None = None) -> None:
    """Create the vhost. ``PUT`` is create-or-update, so this is idempotent."""
    _request("PUT", f"/vhosts/{_esc(vhost)}", json={}, api=api)


def ensure_user(user: str, password: str, *, tags: str = "",
                api: httpx.Client | None = None) -> None:
    """Create or reset a user, password pre-hashed. Idempotent, and unlike
    ``rabbitmqctl add_user`` it needs no delete-then-recreate to change one."""
    _request("PUT", f"/users/{_esc(user)}", json={
        "password_hash": password_hash(password),
        "hashing_algorithm": "rabbit_password_hashing_sha256",
        "tags": tags,
    }, api=api)


def set_permissions(vhost: str, user: str, *, configure: str = ".*",
                    write: str = ".*", read: str = ".*",
                    api: httpx.Client | None = None) -> None:
    """Grant the user full rights on that vhost, and nowhere else."""
    _request("PUT", f"/permissions/{_esc(vhost)}/{_esc(user)}", json={
        "configure": configure, "write": write, "read": read,
    }, api=api)


def ensure_tenant(vhost: str, user: str, password: str,
                  *, api: httpx.Client | None = None) -> None:
    """The whole broker side of one tenant, in the order a fresh one needs."""
    ensure_vhost(vhost, api=api)
    ensure_user(user, password, api=api)
    set_permissions(vhost, user, api=api)


def delete_user(user: str, *, api: httpx.Client | None = None) -> bool:
    """Delete a user; already absent is success, not an error."""
    return _request("DELETE", f"/users/{_esc(user)}",
                    ok_missing=True, api=api).status_code != 404


def delete_vhost(vhost: str, *, api: httpx.Client | None = None) -> bool:
    """Delete a vhost and everything in it; already absent is success."""
    return _request("DELETE", f"/vhosts/{_esc(vhost)}",
                    ok_missing=True, api=api).status_code != 404


def list_vhosts(*, api: httpx.Client | None = None) -> list[str]:
    return [v["name"] for v in _request("GET", "/vhosts", api=api).json()]


def list_users(*, api: httpx.Client | None = None) -> list[str]:
    return [u["name"] for u in _request("GET", "/users", api=api).json()]


def queues(vhost: str, *, api: httpx.Client | None = None) -> list[dict]:
    """Queue depths for one vhost: name, ready, unacked, consumers.

    Diagnostics now, and the ``queues`` field of ``fleet_status`` later.
    """
    rows = _request(
        "GET",
        f"/queues/{_esc(vhost)}?columns=name,messages_ready,"
        "messages_unacknowledged,consumers",
        ok_missing=True, api=api,
    )
    if rows.status_code == 404:
        return []
    return [
        {
            "name": row.get("name", ""),
            "ready": row.get("messages_ready", 0),
            "unacked": row.get("messages_unacknowledged", 0),
            "consumers": row.get("consumers", 0),
        }
        for row in rows.json()
    ]


def whoami(*, api: httpx.Client | None = None) -> dict:
    """``GET /whoami`` — the cheapest check that the credentials work."""
    return _request("GET", "/whoami", api=api).json()
