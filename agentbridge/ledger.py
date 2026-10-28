# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-task metadata ledger: one JSON object per line, append-only.

The bridge appends a line when a task finishes; the service reads it back for
``task_history`` and ``usage_summary``. Two properties matter:

**It lives outside the workspace.** A read-write agent can rewrite anything it
can reach, so a ledger inside the workspace would be a record the subject of
the record can edit. It sits with the tenant records at mode 0600 instead.

**It holds metadata only** — ids, model, counters, timings, cost. Never task
text, agent output, or tool inputs. That is what makes it safe to keep
indefinitely and to return to a tool caller.

It also does not depend on anyone calling ``collect``: a task nobody collected
is still recorded.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Rotate at 10 MB, keeping one generation. A ledger line is a few hundred
# bytes, so this is tens of thousands of tasks.
MAX_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 500

_RELATIVE = re.compile(r"^(\d+)([dhm])$")
_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_since(spec: str | None) -> datetime | None:
    """``"7d"``/``"24h"``/``"30m"``, or an ISO date or timestamp, to a UTC bound."""
    if spec is None or spec == "":
        return None
    text = str(spec).strip()
    if m := _RELATIVE.match(text):
        amount, unit = int(m.group(1)), m.group(2)
        return datetime.now(timezone.utc) - timedelta(**{_UNITS[unit]: amount})
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"invalid since {spec!r}: use 7d, 24h, 30m, a date (2026-10-28) "
            f"or an ISO timestamp"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _rotated(path: Path) -> Path:
    return path.with_name(path.name + ".1")


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialise append and rotation across the tenant's workers.

    The lock is a sidecar file, not the ledger itself: rotation renames the
    ledger, and a lock held on the renamed inode would protect nothing.
    """
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def append(path: Path | str, record: dict) -> None:
    """Append one record, rotating first if the ledger has reached MAX_BYTES."""
    path = Path(path)
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    with _locked(path):
        try:
            if path.stat().st_size >= MAX_BYTES:
                os.replace(path, _rotated(path))
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # One write of one line: O_APPEND makes concurrent writers append
            # whole lines rather than interleave them.
            os.write(fd, line.encode())
        finally:
            os.close(fd)


def _tail_lines(path: Path, max_lines: int, block: int = 65536) -> list[str]:
    """The last ``max_lines`` complete lines, read from the end of the file."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    chunks: list[bytes] = []
    newlines = 0
    with open(path, "rb") as fh:
        position = size
        while position > 0 and newlines <= max_lines:
            step = min(block, position)
            position -= step
            fh.seek(position)
            chunk = fh.read(step)
            chunks.insert(0, chunk)
            newlines += chunk.count(b"\n")
    data = b"".join(chunks)
    lines = data.decode("utf-8", errors="replace").splitlines()
    # The first line may be a fragment of a line the window cut in half.
    if newlines > max_lines and lines:
        lines = lines[1:]
    return lines[-max_lines:]


def records(
    path: Path | str,
    limit: int = 50,
    since: str | None = None,
    role: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    """The most recent records first, newest-to-oldest, filters applied.

    A malformed line is skipped rather than raised: a truncated last line
    (a worker killed mid-write) must not make the whole history unreadable.
    """
    limit = max(1, min(int(limit), MAX_RECORDS))
    bound = parse_since(since)
    path = Path(path)

    def matches(record: dict) -> bool:
        if role is not None and record.get("role") != role:
            return False
        if kind is not None and record.get("kind", "task") != kind:
            return False
        if bound is not None:
            stamp = record.get("ts_end") or record.get("ts_start")
            try:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return False
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < bound:
                return False
        return True

    found: list[dict] = []
    # Read the live ledger first, then the rotated generation if a filter is
    # hungry enough to exhaust it.
    for candidate in (path, _rotated(path)):
        # Over-read: filters may reject most of what the window holds.
        for line in reversed(_tail_lines(candidate, max(limit * 4, 200))):
            if len(found) >= limit:
                return found
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and matches(record):
                found.append(record)
        if len(found) >= limit:
            break
    return found[:limit]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 3)


GROUP_KEYS = ("model", "role", "day", "worker", "backend", "kind")


def _group_of(record: dict, by: str) -> str:
    if by == "day":
        stamp = str(record.get("ts_start") or record.get("ts_end") or "")
        return stamp[:10] or "unknown"
    return str(record.get(by) or "unknown")


def summarize(source: Iterable[dict], by: str = "model") -> dict:
    """Task counts, failures, duration percentiles, tokens and cost per group."""
    if by not in GROUP_KEYS:
        raise ValueError(
            f"invalid by {by!r}: use one of {', '.join(GROUP_KEYS)}"
        )
    groups: dict[str, dict] = {}
    durations: dict[str, list[float]] = {}
    estimated = False

    for record in source:
        key = _group_of(record, by)
        bucket = groups.setdefault(key, {
            by if by != "day" else "day": key,
            "tasks": 0, "failed": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "cost_usd": 0.0,
        })
        bucket["tasks"] += 1
        if record.get("rc") not in (0, None):
            bucket["failed"] += 1
        for field in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_creation_tokens"):
            value = record.get(field)
            if isinstance(value, (int, float)):
                bucket[field] += value
        cost = record.get("cost_usd")
        if isinstance(cost, (int, float)):
            bucket["cost_usd"] += cost
        if record.get("cost_estimated"):
            estimated = True
        duration = record.get("duration_ms")
        if isinstance(duration, (int, float)):
            durations.setdefault(key, []).append(duration / 1000.0)

    for key, bucket in groups.items():
        series = durations.get(key, [])
        bucket["p50_s"] = _percentile(series, 0.50)
        bucket["p95_s"] = _percentile(series, 0.95)
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)

    rows = sorted(groups.values(), key=lambda b: (-b["tasks"], str(b.get(by))))
    return {
        "by": by,
        "groups": rows,
        "totals": {
            "tasks": sum(b["tasks"] for b in rows),
            "failed": sum(b["failed"] for b in rows),
            "cost_usd": round(sum(b["cost_usd"] for b in rows), 6),
            "input_tokens": sum(b["input_tokens"] for b in rows),
            "output_tokens": sum(b["output_tokens"] for b in rows),
        },
        # True if any task ran on a subscription credential, where claude's
        # cost figure is notional rather than money billed.
        "cost_estimated": estimated,
    }
