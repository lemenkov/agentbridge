# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Declared outputs: the files a task promises to produce.

A task can say up front what it will leave behind — ``expect=["review.md"]``
— and the bridge checks, on the host, whether it actually did. This is the
same idea as ``run_check``: an agent's own account of its work is the least
reliable evidence available, and "I wrote the review" is a sentence a model
will produce whether or not a file exists.

Every task gets its own writable directory, mounted at ``/out`` in the leaf.
That directory is writable even for read-only roles, which is the point: a
reviewer can deliver a verdict without being given write access to the
workspace it reviewed.

The checks are deliberately picky. A missing file is the obvious failure, but
an **empty** file, a **symlink** (an agent pointing ``/out/review.md`` at
something else entirely) and a **stale** file (one that predates the task, so
it is a previous run's output being passed off as this one's) are all failures
too, and each is reported distinctly so the orchestrator knows which.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

MAX_PATHS = 16
MAX_PATH_LENGTH = 255

# What a declared output can be, worst to best. `ok` is the only pass.
OK = "ok"
MISSING = "missing"
EMPTY = "empty"
SYMLINK = "symlink"
STALE = "stale"
NOT_A_FILE = "not_a_file"


class ExpectError(Exception):
    """A caller-fixable problem with a declared output list."""


def validate(paths) -> list[str]:
    """Validate the declared paths. Relative, no ``..``, no absolutes.

    Validated in the bridge as well as in the service, because a task message
    can be published by anything holding the tenant's bus credential — the
    service is not the only possible sender.
    """
    if paths is None:
        return []
    if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
        raise ExpectError("expect must be a list of relative file names")
    if len(paths) > MAX_PATHS:
        raise ExpectError(f"expect takes at most {MAX_PATHS} paths")
    clean: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ExpectError("every expect entry must be a non-empty string")
        if len(raw) > MAX_PATH_LENGTH:
            raise ExpectError(f"expect path {raw[:40]!r}… is too long")
        if "\x00" in raw:
            raise ExpectError("expect paths must not contain NUL")
        path = PurePosixPath(raw)
        if path.is_absolute():
            raise ExpectError(f"expect path {raw!r} must be relative to /out")
        if ".." in path.parts:
            raise ExpectError(f"expect path {raw!r} must not contain '..'")
        if raw in clean:
            continue
        clean.append(raw)
    return clean


def check_one(out_dir: Path, name: str, not_before: float | None = None) -> str:
    """One declared output's status, checked on the host side."""
    root = Path(out_dir).resolve()
    target = root / name
    try:
        st = target.lstat()
    except (OSError, ValueError):
        return MISSING
    # Checked before anything else: a symlink's target may be outside /out
    # entirely, and following it would be the agent choosing what counts as
    # its own output.
    if target.is_symlink():
        return SYMLINK
    if not target.is_file():
        return NOT_A_FILE
    if st.st_size == 0:
        return EMPTY
    if not_before is not None and st.st_mtime < not_before:
        # Older than the task that promised it: a previous run's file.
        return STALE
    return OK


def check(out_dir: Path, paths: list[str],
          not_before: float | None = None) -> dict[str, str]:
    return {name: check_one(out_dir, name, not_before) for name in paths}


def all_ok(statuses: dict[str, str]) -> bool:
    return all(status == OK for status in statuses.values())


def prompt_line(paths: list[str]) -> str:
    """What the task message tells the agent, so the promise is explicit."""
    if not paths:
        return ""
    names = ", ".join(f"/out/{p}" for p in paths)
    return (
        f"\n\nDeclared outputs — write exactly these files, non-empty, before "
        f"you finish: {names}. They are checked on the host after you exit; a "
        f"missing, empty or symlinked file fails the task regardless of what "
        f"you report."
    )
