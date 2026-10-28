#!/bin/sh
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Undo a task's committed work, using the checkpoint it left behind. Prints
# one JSON object on stdout.
#
#     sh rollback.sh <task_id> [root] [repo|ALL] [force]
#
# Runs INSIDE A CONTAINER for the reason given in checkpoint.sh (I-3).
#
# Two things it deliberately does not do:
#
#   * It never runs `git clean`. Untracked files are often the only copy of
#     something, and an undo that deletes them is not an undo.
#   * It refuses a repository that was already dirty when the checkpoint was
#     taken, unless forced: the pre ref cannot describe uncommitted changes,
#     so resetting to it would discard work the checkpoint never captured.
#
# Before resetting, it records the current tip as refs/agentbridge/post/<id>,
# so the rollback itself can be undone.

set -eu

TASK_ID="${1:?usage: rollback.sh <task_id> [root] [repo|ALL] [force]}"
ROOT="${2:-/workspace}"
ONLY="${3:-ALL}"
FORCE="${4:-no}"

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

find "$ROOT" -maxdepth 3 -name .git -type d 2>/dev/null | sort | while read -r gitdir; do
    repo=$(dirname "$gitdir")
    rel=$(printf '%s' "$repo" | sed -e "s|^$ROOT||" -e 's|^/||')
    [ -n "$rel" ] || rel="."

    if [ "$ONLY" != "ALL" ] && [ "$ONLY" != "$rel" ]; then
        continue
    fi

    emit() {
        [ $first -eq 1 ] || printf ','
        first=0
        printf '{"repo":"%s","status":"%s"%s}' \
            "$(json_escape "$rel")" "$1" "${2:-}"
    }

    pre=$(GIT -C "$repo" rev-parse --verify --quiet "refs/agentbridge/pre/$TASK_ID" 2>/dev/null || true)
    if [ -z "$pre" ]; then
        emit "no_checkpoint"
        continue
    fi

    sidecar="$gitdir/agentbridge/$TASK_ID.json"
    branch=""
    was_dirty="false"
    if [ -f "$sidecar" ]; then
        branch=$(sed -n 's/.*"branch":"\([^"]*\)".*/\1/p' "$sidecar")
        if grep -q '"dirty":true' "$sidecar"; then
            was_dirty="true"
        fi
    fi

    if [ "$was_dirty" = "true" ] && [ "$FORCE" != "force" ]; then
        emit "refused_dirty" ',"note":"the worktree was already dirty when the checkpoint was taken; those changes are not in the checkpoint. Re-run with force=true to discard them."'
        continue
    fi

    now=$(GIT -C "$repo" rev-parse HEAD 2>/dev/null || true)
    # Recorded before anything is moved, so the rollback is itself undoable.
    [ -z "$now" ] || GIT -C "$repo" update-ref "refs/agentbridge/post/$TASK_ID" "$now"

    current=$(GIT -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [ -n "$branch" ] && [ "$branch" = "$current" ]; then
        # On the recorded branch: move the worktree and the branch together.
        GIT -C "$repo" reset --hard "$pre" >/dev/null
        emit "reset" ",\"from\":\"$now\",\"to\":\"$pre\",\"branch\":\"$(json_escape "$branch")\""
    elif [ -n "$branch" ]; then
        # Checked out elsewhere: move the branch only, and leave the worktree.
        GIT -C "$repo" update-ref "refs/heads/$branch" "$pre"
        emit "branch_reset" ",\"from\":\"$now\",\"to\":\"$pre\",\"branch\":\"$(json_escape "$branch")\",\"note\":\"HEAD is on a different branch; only the recorded branch was moved\""
    else
        GIT -C "$repo" reset --hard "$pre" >/dev/null
        emit "reset_detached" ",\"from\":\"$now\",\"to\":\"$pre\""
    fi
done

printf ']}\n'
