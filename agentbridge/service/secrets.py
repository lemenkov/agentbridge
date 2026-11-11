# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Anthropic credentials as podman secrets in the service user's store.

The value only ever travels on a child process's stdin: never argv (visible in
``ps``), never a file, never a log line. Not even its length is recorded,
since that distinguishes an API key from an OAuth token.

Note for anyone reading this expecting encryption: podman's default ``file``
driver stores secrets **unencrypted** under the user's data directory. They
are protected by file permissions alone, so the service user and root can read
them. That is the invariant we actually hold (I-1 in the plan), and it is what
the README must say.

``agentbridge-admin`` runs as root while the store belongs to the service
user, so every call takes an optional ``runner`` that wraps the argv (with
``runuser``) instead of running it directly.

The **default** runner goes through ``systemd-run --user`` (see ``sdrun``), so
no podman call here is issued from the service's own mount namespace. That is
invariant I-6, and it is why ``direct_runner`` is no longer the default: a
direct ``podman secret ls`` is exactly what broke every container on
2026-11-03 (review log 49). ``direct_runner`` is kept for callers that have
already left the service's namespace — ``agentbridge-admin`` runs as root and
reaches the store through ``runuser``, so it supplies its own.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Protocol

from . import sdrun


class SecretError(RuntimeError):
    """podman refused. The message never contains the secret's value."""


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], stdin: bytes | None = None
                 ) -> subprocess.CompletedProcess: ...


def direct_runner(argv: Sequence[str], stdin: bytes | None = None
                  ) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), input=stdin, capture_output=True, check=False)


def _run(runner: Runner, argv: Sequence[str], stdin: bytes | None = None,
         *, allow_missing: bool = False) -> subprocess.CompletedProcess:
    proc = runner(argv, stdin)
    if proc.returncode != 0:
        err = (proc.stderr or b"")
        err = err.decode(errors="replace") if isinstance(err, bytes) else str(err)
        if allow_missing and "no such secret" in err.lower():
            return proc
        # argv is safe to show: the value went in on stdin.
        raise SecretError(f"{' '.join(argv)} failed: {err.strip()[:200]}")
    return proc


_replace_support: bool | None = None


def _supports_replace(runner: Runner = sdrun.runner) -> bool:
    """Whether the installed podman has ``secret create --replace`` (5.0+).

    Without it, replacing means ``rm`` then ``create``, which leaves a window
    where the worker has no credential — so prefer ``--replace`` when present.

    Takes the caller's runner rather than reaching for ``direct_runner``: this
    is still a ``podman`` invocation, and ``--help`` is not obviously too early
    to start the pause process. Not worth betting the fleet on. Cached in a
    module global rather than with ``lru_cache`` so the runner argument does
    not become part of the cache key — the answer is a property of the
    installed podman, not of who asked.
    """
    global _replace_support
    if _replace_support is None:
        proc = runner(["podman", "secret", "create", "--help"])
        _replace_support = b"--replace" in (proc.stdout or b"")
    return _replace_support


def create(name: str, value: str, *, runner: Runner = sdrun.runner) -> None:
    """Store (or replace) a secret. The value is written to stdin and dropped."""
    if not value:
        raise SecretError("refusing to store an empty secret")
    data = value.encode()
    if _supports_replace(runner):
        _run(runner, ["podman", "secret", "create", "--replace", name, "-"], data)
        return
    _run(runner, ["podman", "secret", "rm", name], allow_missing=True)
    _run(runner, ["podman", "secret", "create", name, "-"], data)


def remove(name: str, *, runner: Runner = sdrun.runner) -> bool:
    """Revoke a secret. Already absent is success, not an error."""
    proc = _run(runner, ["podman", "secret", "rm", name], allow_missing=True)
    return proc.returncode == 0


def names(*, runner: Runner = sdrun.runner) -> set[str]:
    """Names of the secrets in the store. Values are never listed."""
    proc = _run(runner, ["podman", "secret", "ls", "--format", "{{.Name}}"])
    out = proc.stdout or b""
    return set((out.decode() if isinstance(out, bytes) else out).split())
