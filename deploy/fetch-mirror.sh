#!/bin/sh
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Update a workspace clone from a host-owned bare mirror. Prints one JSON
# object on stdout.
#
#     sh fetch-mirror.sh <repo-relative-path> [root] [mirror]
#
# Runs INSIDE A CONTAINER, for the reason in checkpoint.sh (I-3): the
# repository being updated is writable by an agent, and a repository is a
# program — core.fsmonitor, hooks and filter drivers all name commands git
# executes. The fetch therefore happens where that can only affect the
# sandbox.
#
# The mirror is mounted read-only and holds no credential: the host has
# already done the authenticated fetch into it. Nothing in this container can
# reach the network at all (--network=none).

set -eu

REL="${1:?usage: fetch-mirror.sh <repo-relative-path> [root] [mirror]}"
ROOT="${2:-/workspace}"
MIRROR="${3:-/mirror}"

GIT() {
    # protocol.file.allow is needed because the mirror is a local path, and
    # git refuses the file transport by default in some operations. It is our
    # own read-only mount, inside a sandbox with no network.
    git -c "safe.directory=*" -c core.fsmonitor=false \
        -c core.hooksPath=/dev/null -c protocol.file.allow=always "$@"
}
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0

REPO="$ROOT/$REL"
[ -d "$REPO/.git" ] || { printf '{"error":"not a git repository"}\n'; exit 1; }

before=$(GIT -C "$REPO" rev-parse --verify --quiet HEAD 2>/dev/null || true)

# --prune so deleted upstream branches go; --no-tags because tags are fetched
# explicitly below, and letting a fetch invent tag refs surprises people.
GIT -C "$REPO" fetch --prune --no-tags "$MIRROR" \
    "+refs/heads/*:refs/remotes/origin/*" >/dev/null 2>&1
GIT -C "$REPO" fetch --no-tags "$MIRROR" "+refs/tags/*:refs/tags/*" \
    >/dev/null 2>&1 || true

after=$(GIT -C "$REPO" rev-parse --verify --quiet HEAD 2>/dev/null || true)
branches=$(GIT -C "$REPO" for-each-ref --format='%(refname:short)' \
    refs/remotes/origin 2>/dev/null | wc -l | tr -d ' ')

# HEAD is deliberately untouched: this updates remote-tracking refs and lets
# the agent (or the human) decide what to merge. A fetch that moved the
# working tree would be a different, destructive operation.
printf '{"path":"%s","head":"%s","head_unchanged":%s,"remote_branches":%s}\n' \
    "$REL" "$after" \
    "$([ "$before" = "$after" ] && echo true || echo false)" \
    "${branches:-0}"
