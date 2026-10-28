# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""A per-caller sliding-window limit, held in memory.

It guards the tools that cost something irreversible — storing a credential,
changing a password — so a confused or injected orchestrator cannot hammer
them. Memory only: the service is a single process, and a limit that survives
a restart would need state we deliberately do not keep.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

WINDOW_SECONDS = 60.0
DEFAULT_LIMIT = 5

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


class RateLimited(Exception):
    """The caller has used up its allowance; says when to try again."""

    def __init__(self, retry_after: float, limit: int):
        self.retry_after = max(1, int(retry_after + 0.999))
        super().__init__(
            f"rate limit reached ({limit} per minute): try again in "
            f"{self.retry_after}s"
        )


def check(user: str, action: str, *, limit: int = DEFAULT_LIMIT,
          now: float | None = None) -> None:
    """Record one use of ``action`` by ``user``, or raise ``RateLimited``.

    A refused call is not recorded, so a caller that waits is not punished for
    having tried.
    """
    now = time.monotonic() if now is None else now
    hits = _hits[(user, action)]
    while hits and now - hits[0] >= WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= limit:
        raise RateLimited(WINDOW_SECONDS - (now - hits[0]), limit)
    hits.append(now)


def reset(user: str | None = None) -> None:
    """Forget history, for one user or all of them. Used by tests."""
    if user is None:
        _hits.clear()
        return
    for key in [k for k in _hits if k[0] == user]:
        del _hits[key]
