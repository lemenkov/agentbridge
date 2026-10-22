# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Leaf image for read-only roles (researcher). `claude` itself is NOT baked in:
# the service bind-mounts the host's native binary (CLAUDE_BIN) read-only, so
# the image never needs a rebuild when claude updates.
#
# Build as the service user (images live in its rootless store):
#   runuser -u agentbridge -- env XDG_RUNTIME_DIR=/run/user/$(id -u agentbridge) \
#       podman build -t agentbridge-base -f images/base.Containerfile images/
FROM registry.fedoraproject.org/fedora:44
RUN dnf install -y --setopt=install_weak_deps=False \
        ca-certificates git-core ripgrep jq \
    && dnf clean all
