# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Leaf image for code roles (implementer, reviewer): base tools plus git and a
# Python / C toolchain. Implementers commit signed-off with the per-tenant
# gitconfig the service mounts; they never push.
#
#   runuser -u agentbridge -- env XDG_RUNTIME_DIR=/run/user/$(id -u agentbridge) \
#       podman build -t agentbridge-pydev -f images/pydev.Containerfile images/
FROM localhost/agentbridge-base
RUN dnf install -y --setopt=install_weak_deps=False \
        git python3 python3-pip python3-pytest ruff gcc make diffutils patch \
    && dnf clean all
