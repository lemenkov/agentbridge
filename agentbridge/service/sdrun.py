# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Run podman outside this process's mount namespace (invariant I-6).

Why this module exists, in one paragraph. Rootless podman keeps **one pause
process per user** (``catatonit -P``) and every later ``podman run`` joins its
user and mount namespaces. Whatever namespace the pause process is born into
is therefore inherited by every container started afterwards — for the life of
the pause process, not the life of the call that created it. On 2026-11-03 the
service had ``ProtectKernelTunables=yes``, which covers ``/proc/sys`` and nine
other ``/proc`` paths with read-only binds; a ``podman secret ls`` issued
directly from the service birthed the pause process inside that sandbox, and
from then on the kernel refused every container's own ``/proc`` mount
(``mount_too_revealing()``, ``VFS: Mount too revealing``). Every task failed
``rc=126`` until the directive was removed. See review log 49.

Removing the directive fixed it. It did not make it *stay* fixed: the next
person to harden the unit reintroduces the outage, and the failure appears
one podman call later, in a different subsystem, with an error that names
neither the unit nor the directive.

So the invariant is structural rather than remembered: **no podman command
runs in this process's mount namespace.** Every one goes through
``systemd-run --user``, which starts it from the user manager instead, where
no service sandboxing applies. The unit file then cannot break containers
however it is hardened, and the guarantee holds without anyone having to know
the story above.

Verify it on the host by reading the pause process's mount table, not the unit
file — the unit file is what lied to us last time::

    pgrep -u agentbridge -f 'catatonit -P'
    grep ' proc ' /proc/<pid>/mountinfo      # must show no read-only binds

Two call shapes are offered because the service needs both: `runner` for the
synchronous credential path in ``secrets``, which must put a secret's value on
a child's **stdin** and nowhere else, and `run` for the async leaf and query
paths. Both take the bare argv — ``["podman", "secret", "ls", ...]`` — and wrap
it here, so call sites stay readable and only this module knows about
``systemd-run``.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Sequence

# Long enough that a slow image pull is not killed, short enough that a wedged
# transient unit cannot outlive the request that made it. The caller's own
# timeout still applies; this is the backstop for the case where the caller is
# gone and nobody is left to cancel.
DEFAULT_MAX_SECONDS = 300


def wrap(argv: Sequence[str], *, description: str,
         max_seconds: int | None = DEFAULT_MAX_SECONDS) -> list[str]:
    """``argv`` as a transient user unit, run from the user manager.

    No ``--unit=``: these are one-off calls that may overlap, and a fixed name
    would make two concurrent callers collide. systemd generates a unique one.

    ``--pipe`` passes our stdin, stdout and stderr straight to the command,
    which is what keeps the credential path honest — the value travels on a
    pipe, never in argv (visible in ``ps``) and never through a file.

    ``--quiet`` matters more than it looks: without it systemd-run writes
    "Running as unit ..." to stderr, which ends up quoted in error messages
    and, worse, in the middle of a leaf's JSON diagnostics.
    """
    cmd = [
        "systemd-run", "--user", "--quiet", "--collect", "--wait", "--pipe",
        f"--description={description}",
    ]
    if max_seconds:
        cmd.append(f"--property=RuntimeMaxSec={max_seconds}")
    return [*cmd, *argv]


def runner(argv: Sequence[str], stdin: bytes | None = None
           ) -> subprocess.CompletedProcess:
    """A ``secrets.Runner`` that satisfies I-6.

    Drop-in for ``secrets.direct_runner``: same signature, same return, but the
    command starts from the user manager. ``secrets`` keeps asserting on the
    inner podman argv, so its tests are unaffected by the wrapping.
    """
    wrapped = wrap(argv, description=f"agentbridge {' '.join(argv[:3])}")
    return subprocess.run(wrapped, input=stdin, capture_output=True,
                          check=False)


async def run(argv: Sequence[str], *, description: str,
              timeout: float | None = None,
              max_seconds: int | None = DEFAULT_MAX_SECONDS,
              ) -> tuple[int, bytes, bytes]:
    """Await ``argv`` as a transient user unit. Returns ``(rc, stdout, stderr)``.

    On timeout the child is killed and the exception propagates; the unit's own
    ``RuntimeMaxSec`` then reaps anything that outlives us, so a wedged podman
    cannot leak a running container past the request.
    """
    proc = await asyncio.create_subprocess_exec(
        *wrap(argv, description=description, max_seconds=max_seconds),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out or b"", err or b""
