# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Which model backends exist, read from one root-owned file.

A backend is where a worker's `claude` sends its requests. Today there is only
one kind that works — `anthropic`, the hosted API — and a host with no
`backends.toml` gets exactly that, so this module is invisible until an
operator writes the file.

The registry is separate from `spawn_worker` for one reason: a bad backend
definition should stop the service at startup, with the reason, rather than
surface as a worker that spawns and then fails every task. `load()` therefore
raises on anything it cannot vouch for, and `main()` lets that propagate.

The file is **root-owned, 0644, and holds no secrets.** A backend that needs a
credential names an operator podman secret; the value stays in podman. Both
this service and `agentbridge-netpolicy` read the file, which is why the
endpoint list has to be declarative rather than computed here.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from . import config

KINDS = ("anthropic", "ollama", "anthropic-compatible")

# Kinds whose requests leave this host for an address the operator chose, so
# they need somewhere to send them and a list of what may be asked for.
NEEDS_ENDPOINTS = ("ollama", "anthropic-compatible")


class BackendError(Exception):
    """A backends.toml this module will not vouch for."""


@dataclass(frozen=True)
class Endpoint:
    url: str                        # scheme://host[:port], nothing else
    max_workers: int | None = None  # live workers pinned here, across ALL tenants

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


@dataclass(frozen=True)
class Backend:
    name: str
    kind: Literal["anthropic", "ollama", "anthropic-compatible"]
    endpoints: tuple[Endpoint, ...] = ()
    models: tuple[str, ...] = ()
    default_model: str | None = None
    credential: str | None = None       # "anthropic-compatible" only
    tenants: tuple[str, ...] = ()       # empty = every tenant
    task_timeout: int | None = None

    def allows(self, tenant_name: str) -> bool:
        """Empty `tenants` means every tenant, which is the common case."""
        return not self.tenants or tenant_name in self.tenants

    def model_or_default(self, model: str | None) -> str | None:
        """The model to run, or None for `anthropic`, which validates its own."""
        if self.kind == "anthropic":
            return model
        return model or self.default_model


# The one backend every host has. Defined here rather than in a file so that
# an absent, unreadable or empty backends.toml still yields a working service.
ANTHROPIC = Backend(name="anthropic", kind="anthropic")


# --------------------------------------------------------------- validation


def _require_url(name: str, url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise BackendError(f"backend {name!r}: an endpoint needs a url")
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BackendError(
            f"backend {name!r}: endpoint {url!r} must be http:// or https://")
    if not parts.hostname:
        raise BackendError(f"backend {name!r}: endpoint {url!r} has no host")
    # A path would be silently ignored by the callers that append their own
    # ("/api/tags", "/v1/models"), and userinfo is a credential in a file that
    # is deliberately world-readable.
    if parts.path not in ("", "/"):
        raise BackendError(
            f"backend {name!r}: endpoint {url!r} must have no path")
    if parts.query or parts.fragment:
        raise BackendError(
            f"backend {name!r}: endpoint {url!r} must have no query or fragment")
    if parts.username or parts.password:
        raise BackendError(
            f"backend {name!r}: endpoint {url!r} must not carry userinfo — "
            f"this file is world-readable; use `credential`")
    try:
        parts.port
    except ValueError:
        raise BackendError(
            f"backend {name!r}: endpoint {url!r} has a bad port") from None
    return url.rstrip("/")


def _is_private_host(host: str) -> bool | None:
    """True/False for an IP literal, None for a name we will not resolve here.

    Resolving a name at config-load time would make the service refuse to start
    whenever DNS is briefly down, and the answer could change afterwards
    anyway, so a name is not judged here. The real enforcement is the nftables
    allowlist (0.1b): the service and every leaf run as `agentbridge` and can
    only reach what that allows, so a plaintext endpoint pointing somewhere it
    should not simply fails to connect. Tightened in build step 12, where the
    health probe has to resolve the host regardless.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _check_plaintext(name: str, url: str, credential: str | None) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    private = _is_private_host(parts.hostname or "")
    if private is False:
        raise BackendError(
            f"backend {name!r}: endpoint {url!r} is plaintext to a public "
            f"address; use https://")
    if private is None and credential:
        # A credential is about to be sent somewhere. Unencrypted is not a
        # judgement call, and a hostname is exactly the case we cannot check.
        raise BackendError(
            f"backend {name!r}: endpoint {url!r} is plaintext and this backend "
            f"has a credential; use https://")


def _endpoints(name: str, raw: object) -> tuple[Endpoint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise BackendError(f"backend {name!r}: `endpoints` must be a list")
    out: list[Endpoint] = []
    for item in raw:
        if not isinstance(item, dict):
            raise BackendError(
                f"backend {name!r}: each endpoint must be a table with a url")
        unknown = set(item) - {"url", "max_workers"}
        if unknown:
            raise BackendError(
                f"backend {name!r}: unknown endpoint key(s) "
                f"{', '.join(sorted(unknown))}")
        url = _require_url(name, item.get("url"))
        cap = item.get("max_workers")
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool)
                                or cap < 1):
            raise BackendError(
                f"backend {name!r}: endpoint {url!r} max_workers must be a "
                f"positive integer")
        out.append(Endpoint(url=url, max_workers=cap))
    seen = [e.url for e in out]
    duplicate = {u for u in seen if seen.count(u) > 1}
    if duplicate:
        raise BackendError(
            f"backend {name!r}: endpoint {sorted(duplicate)[0]!r} is listed twice")
    return tuple(out)


def _strings(name: str, key: str, raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise BackendError(f"backend {name!r}: `{key}` must be a list of strings")
    values = tuple(s.strip() for s in raw)
    if any(not s for s in values):
        raise BackendError(f"backend {name!r}: `{key}` has an empty entry")
    return values


def _backend(name: str, table: dict) -> Backend:
    if not isinstance(table, dict):
        raise BackendError(f"backend {name!r}: must be a table")
    unknown = set(table) - {"kind", "endpoints", "models", "default_model",
                            "credential", "tenants", "task_timeout"}
    if unknown:
        raise BackendError(
            f"backend {name!r}: unknown key(s) {', '.join(sorted(unknown))}")

    kind = table.get("kind")
    if kind not in KINDS:
        raise BackendError(
            f"backend {name!r}: kind must be one of {', '.join(KINDS)}")

    endpoints = _endpoints(name, table.get("endpoints"))
    models = _strings(name, "models", table.get("models"))
    tenants = _strings(name, "tenants", table.get("tenants"))
    credential = table.get("credential")
    default_model = table.get("default_model")
    task_timeout = table.get("task_timeout")

    if credential is not None:
        if kind != "anthropic-compatible":
            raise BackendError(
                f"backend {name!r}: `credential` is only for "
                f"anthropic-compatible; {kind} needs no token here")
        if not isinstance(credential, str) or not credential.strip():
            raise BackendError(
                f"backend {name!r}: `credential` must name a podman secret")
        credential = credential.strip()

    if kind == "anthropic":
        # The hosted API has one address and validates model names itself; a
        # list here would be a second place to keep in step with Anthropic's.
        for key, value in (("endpoints", endpoints), ("models", models)):
            if value:
                raise BackendError(
                    f"backend {name!r}: kind anthropic takes no `{key}`")
        if default_model is not None:
            raise BackendError(
                f"backend {name!r}: kind anthropic takes no `default_model`; "
                f"pass `model` to spawn_worker instead")
    if kind in NEEDS_ENDPOINTS:
        if not endpoints:
            raise BackendError(f"backend {name!r}: kind {kind} needs `endpoints`")
        if not models:
            # The bridge validates a task's model against this list, so an
            # empty one would accept nothing.
            raise BackendError(f"backend {name!r}: kind {kind} needs `models`")
    if kind == "anthropic-compatible" and not credential:
        raise BackendError(
            f"backend {name!r}: kind anthropic-compatible needs a `credential`")

    if default_model is not None:
        if not isinstance(default_model, str) or default_model not in models:
            raise BackendError(
                f"backend {name!r}: default_model {default_model!r} is not in "
                f"`models`")
    elif models:
        default_model = models[0]

    if task_timeout is not None and (not isinstance(task_timeout, int)
                                     or isinstance(task_timeout, bool)
                                     or task_timeout < 1):
        raise BackendError(
            f"backend {name!r}: task_timeout must be a positive integer")

    for endpoint in endpoints:
        _check_plaintext(name, endpoint.url, credential)

    return Backend(name=name, kind=kind, endpoints=endpoints, models=models,
                   default_model=default_model, credential=credential,
                   tenants=tenants, task_timeout=task_timeout)


# ------------------------------------------------------------------ loading


def load(path: Path | None = None) -> dict[str, Backend]:
    """Read the file and return every backend, `anthropic` always included.

    Raises `BackendError` on anything questionable: a worker that spawns
    against a broken definition fails every task it is given, which is a far
    worse failure than refusing to start.
    """
    path = Path(path) if path is not None else config.BACKENDS_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # The documented default: no file means the hosted API and nothing else.
        return {ANTHROPIC.name: ANTHROPIC}
    except OSError as exc:
        raise BackendError(f"cannot read {path}: {exc}") from None

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BackendError(f"{path} is not valid TOML: {exc}") from None

    out: dict[str, Backend] = {}
    for name, table in data.items():
        if not isinstance(name, str) or not name.strip():
            raise BackendError(f"{path}: a backend needs a name")
        out[name] = _backend(name, table)

    # An operator may describe `anthropic` to pin `tenants` or `task_timeout`
    # on it, but it exists whether they do or not.
    out.setdefault(ANTHROPIC.name, ANTHROPIC)
    return out


_cache: dict[str, Backend] | None = None


def backends(reload: bool = False) -> dict[str, Backend]:
    """The loaded registry, read once. `reload=True` re-reads the file."""
    global _cache
    if _cache is None or reload:
        _cache = load()
    return _cache


def get(name: str) -> Backend:
    try:
        return backends()[name]
    except KeyError:
        known = ", ".join(sorted(backends()))
        raise BackendError(
            f"unknown backend {name!r}; this host has: {known}") from None


def for_tenant(tenant_name: str) -> dict[str, Backend]:
    """Only the backends this tenant may spawn on."""
    return {name: b for name, b in backends().items() if b.allows(tenant_name)}
