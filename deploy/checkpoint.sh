#!/bin/sh
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Record a checkpoint of every git repository in a workspace, so a task's
# changes can be undone. Prints one JSON object on stdout.
#
#     sh checkpoint.sh <task_id> [root]
#
# This runs INSIDE A CONTAINER (plan invariant I-3), never on the host. The
# repositories here are writable by an agent, and a repository is a program:
# core.fsmonitor, core.hooksPath, include.path and filter drivers all name
# commands that git will execute. Running these commands on the host would let
# a repository an agent controls execute code as the service user. In a leaf
# with no network and no credential, it can at worst affect itself.
#
# The -c flags below are defence in depth for the same reason, and
# safe.directory is required because the workspace belongs to a different uid
# than this container's root.

set -eu

TASK_ID="${1:?usage: checkpoint.sh <task_id> [root]}"
ROOT="${2:-/workspace}"

# A function, not a variable: `$GIT` unquoted would be glob-expanded, and
# `safe.directory=*` is exactly the kind of word a stray file name could
# change. Here the arguments are quoted once and cannot be re-interpreted.
GIT() {
    git -c "safe.directory=*" -c core.fsmonitor=false \
        -c core.hooksPath=/dev/null "$@"
}
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

first=1
printf '{"task_id":"%s","repos":[' "$(json_escape "$TASK_ID")"

# A repository at depth 0, 1 or 2 below the root: .git itself sits one level
# deeper, hence maxdepth 3.
find "$ROOT" -maxdepth 3 -name .git -type d 2>/dev/null | sort | while read -r gitdir; do
    repo=$(dirname "$gitdir")
    rel=$(printf '%s' "$repo" | sed -e "s|^$ROOT||" -e 's|^/||')
    [ -n "$rel" ] || rel="."

    head=$(GIT -C "$repo" rev-parse HEAD 2>/dev/null) || continue
    branch=$(GIT -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    # --untracked-files=no on purpose. "Dirty" here must mean exactly what
    # rollback would destroy: uncommitted changes to TRACKED files, which
    # `reset --hard` discards. Untracked files survive a rollback (it never
    # runs `git clean`), so counting them would veto the rollback over files
    # that were never at risk — and an agent leaving one scratch file behind
    # would make its own work un-undoable.
    if [ -n "$(GIT -C "$repo" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        dirty=true
    else
        dirty=false
    fi

    # The ref is what rollback resets to; the sidecar remembers which branch
    # was checked out, which a ref alone cannot say.
    GIT -C "$repo" update-ref "refs/agentbridge/pre/$TASK_ID" "$head"
    mkdir -p "$gitdir/agentbridge"
    cat > "$gitdir/agentbridge/$TASK_ID.json" <<EOJSON
{"task_id":"$(json_escape "$TASK_ID")","head":"$head","branch":"$(json_escape "$branch")","dirty":$dirty}
EOJSON

    [ $first -eq 1 ] || printf ','
    first=0
    printf '{"repo":"%s","head":"%s","branch":"%s","dirty":%s}' \
        "$(json_escape "$rel")" "$head" "$(json_escape "$branch")" "$dirty"
done

printf ']}\n'
