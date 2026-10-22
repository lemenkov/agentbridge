# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""mcp-agentbridge: a multi-tenant MCP service that runs agentbridge fleets.

Every caller (identified by the reverse proxy's ``X-Forwarded-User``) gets
their own RabbitMQ vhost, Anthropic credential, workspace and worker units.
"""
