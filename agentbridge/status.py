# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Worker status files: what each worker is doing, right now.

The bridge rewrites a small JSON file at every state transition. The service
reads the directory to build ``fleet_status``, which is why this is the
bridge's view and not a count of unacked broker messages: for a pool worker,
``unacked`` cannot say *which* worker took a task, while the worker itself
knows.

Every write is atomic — a temp file in the same directory, then
``os.replace`` — so a reader either sees the previous state or the next one,
never a half-written file. Readers therefore need no lock.

Like the ledger, status files hold metadata only and live outside the
workspace: a worker reports on itself, so a read-write agent must not be able
to edit the report.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# As the bridge itself sees them. The service adds `gone` and `failed` from
# systemd, which the bridge cannot report because a dead worker writes nothing.
STATES = ("starting", "idle", "working", "retiring")

# A tool-heavy task would otherwise rewrite the file hundreds of times; a
# second's granularity is plenty for a human watching a page.
MIN_WRITE_INTERVAL = 1.0


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StatusWriter:
    """Accumulates one worker's state and writes it atomically.

    With no path it is inert, so the bridge needs no conditionals: an
    orchestrator running the bridge by hand simply has no status file.
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        agent: str,
        role: str | None = None,
        backend: str = "anthropic",
        options: dict[str, str] | None = None,
        driver: str | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self._last_write = 0.0
        self._fields: dict = {
            "agent": agent,
            "role": role,
            "backend": backend,
            "driver": driver,
            "model": (options or {}).get("model"),
            "effort": (options or {}).get("effort"),
            "state": "starting",
            "tasks_done": 0,
        }

    def set(self, state: str | None = None, *, force: bool = True, **fields) -> None:
        """Update fields and write. ``force=False`` respects the write interval."""
        if state is not None:
            self._fields["state"] = state
        self._fields.update(fields)
        if not force and (time.monotonic() - self._last_write) < MIN_WRITE_INTERVAL:
            return
        self._write()

    def task_started(self, task_id: str | None, options: dict[str, str]) -> None:
        self.set(
            "working",
            task_id=task_id,
            task_started=now_iso(),
            # The previous task's tool is not this task's news.
            last_tool=None,
            last_progress=None,
            model=options.get("model") or self._fields.get("model"),
            effort=options.get("effort") or self._fields.get("effort"),
        )

    def progress(self, tool: str | None, phase: str | None) -> None:
        """Record a tool call.

        A *change* of tool always writes — it is the most useful thing the
        page shows, and throttling it could hide the only tool a short task
        used. Repeats of the same tool, and bare assistant turns, respect the
        write interval.
        """
        changed = tool is not None and tool != self._fields.get("last_tool")
        self.set(
            force=changed,
            last_tool=tool or self._fields.get("last_tool"),
            last_phase=phase,
            last_progress=now_iso(),
        )

    def task_finished(self, rc: int) -> None:
        self._fields["tasks_done"] = int(self._fields.get("tasks_done", 0)) + 1
        self.set("idle", task_id=None, task_started=None, last_rc=rc,
                 last_finished=now_iso())

    def retiring(self) -> None:
        self.set("retiring")

    def remove(self) -> None:
        """Delete the file on a clean exit, so nothing has to sweep it."""
        if self.path is None:
            return
        try:
            self.path.unlink()
        except OSError:
            pass

    def _write(self) -> None:
        if self.path is None:
            return
        self._fields["updated"] = now_iso()
        payload = json.dumps(
            {k: v for k, v in self._fields.items() if v is not None},
            separators=(",", ":"), sort_keys=True,
        ).encode()
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.replace(temp, self.path)
            self._last_write = time.monotonic()
        except OSError:
            # A worker that cannot report its status must still do its work.
            try:
                temp.unlink()
            except OSError:
                pass


def read_dir(directory: str | Path) -> list[dict]:
    """Every status file in a directory, as dicts; unreadable files are skipped."""
    path = Path(directory)
    found = []
    try:
        names = sorted(path.iterdir())
    except OSError:
        return []
    for entry in names:
        if entry.name.startswith(".") or entry.suffix != ".json":
            continue
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Mid-replace readers never see this, but a truncated leftover
            # from a killed process should not break the whole view.
            continue
        if isinstance(record, dict):
            record.setdefault("agent", entry.stem)
            found.append(record)
    return found
