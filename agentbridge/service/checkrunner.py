# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The process a check unit runs: exec the command, record how it ended.

This exists so that **no caller-supplied argument ever reaches a shell**. The
obvious implementation of "run this and save the exit code" is
``sh -c 'podman run …; echo $? > result'``, which would mean interpolating the
caller's argv into a shell string — a command injection with extra steps. Here
the argv is passed through as argv, and the result file is written by Python.

It also means the service holds no in-memory state for a running check: the
unit writes the result itself, so a service restart loses nothing and
``check_result`` is just a file read.

Run as::

    python -m agentbridge.service.checkrunner --result R --log L -- CMD...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: str, payload: dict) -> None:
    temp = f"{path}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode())
    finally:
        os.close(fd)
    # A reader sees either no result or a complete one, never a partial.
    os.replace(temp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checkrunner")
    parser.add_argument("--result", required=True, help="result JSON path")
    parser.add_argument("--log", required=True, help="combined output log path")
    parser.add_argument("--check-id", default=None)
    parser.add_argument("--ledger", default=None,
                        help="append one kind=check line here when finished")
    parser.add_argument("--role", default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given")

    started = _now()
    clock = time.monotonic()
    # Announce the attempt before running it, so a check killed by
    # RuntimeMaxSec (or a reboot) is distinguishable from one never started.
    _write_atomic(args.result, {
        "check_id": args.check_id, "state": "running",
        "started": started, "rc": None,
    })

    rc, error = None, None
    try:
        with open(args.log, "ab", buffering=0) as log:
            completed = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                check=False,
            )
        rc = completed.returncode
    except OSError as exc:
        # The command could not be executed at all (missing binary, bad image
        # reference). That is a check failure, not a service failure.
        error = str(exc)
        rc = 127

    ended = _now()
    duration = round(time.monotonic() - clock, 3)
    _write_atomic(args.result, {
        "check_id": args.check_id,
        "state": "done",
        "started": started,
        "ended": ended,
        "duration_s": duration,
        "rc": rc,
        **({"error": error} if error else {}),
    })

    # The unit records its own ledger line, as the bridge does for a task: no
    # service-side bookkeeping, and a check nobody polled is still recorded.
    if args.ledger:
        try:
            from .. import ledger
            ledger.append(args.ledger, {
                "kind": "check",
                "ts_start": started,
                "ts_end": ended,
                "task_id": args.check_id,
                "role": args.role,
                "rc": rc,
                "duration_ms": int(duration * 1000),
            })
        except Exception as exc:                             # noqa: BLE001
            # A missing ledger line must not change a check's verdict, so this
            # is reported into the check's own log and otherwise ignored.
            try:
                with open(args.log, "a", encoding="utf-8") as log:
                    log.write(f"\n[agentbridge] ledger append failed: {exc}\n")
            except OSError:
                pass
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
