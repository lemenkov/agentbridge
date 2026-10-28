# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The exact set of tools each endpoint exposes.

Written after a real bug: inserting a helper function between ``@mcp.tool``
and the function below it moved the decorator onto the helper, so ``collect``
silently stopped being a tool. Nothing in the suite noticed, because every
other test called the functions directly rather than through the registry.

Two properties are checked here, and both are the kind that fail silently:
a tool disappearing, and an admin tool appearing where a prompt-injected
orchestrator could reach it (invariant I-2).
"""

import asyncio
import unittest

from fastmcp import Client

from agentbridge.service.admin_api import admin_mcp
from agentbridge.service.server import mcp

USER_TOOLS = {
    "whoami", "list_roles", "spawn_worker", "list_workers", "send_task",
    "broadcast", "collect", "retire_worker", "retire_all",
    "workspace_ls", "workspace_read", "workspace_write", "clone_repo",
    "register_credential", "revoke_credential", "set_password",
    "set_git_identity", "fleet_status", "task_history", "usage_summary",
    "run_check", "check_result", "rollback_workspace",
    "list_git_credentials", "test_git_credential", "fetch_repo",
}

ADMIN_TOOLS = {"provision_user", "deprovision_user", "list_users"}


def names(server) -> set[str]:
    async def ask():
        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}
    return asyncio.run(ask())


class ToolSurface(unittest.TestCase):
    def test_the_user_endpoint_exposes_exactly_these(self):
        found = names(mcp)
        self.assertEqual(found - USER_TOOLS, set(), "unexpected tool exposed")
        self.assertEqual(USER_TOOLS - found, set(), "tool missing")

    def test_the_admin_endpoint_exposes_exactly_these(self):
        self.assertEqual(names(admin_mcp), ADMIN_TOOLS)

    def test_no_admin_tool_is_reachable_from_the_user_endpoint(self):
        # Invariant I-2: an orchestrator reading untrusted agent output must
        # hold no handle to the tools that create and delete tenants.
        self.assertEqual(names(mcp) & ADMIN_TOOLS, set())

    def test_no_private_helper_leaked_as_a_tool(self):
        self.assertEqual(
            {n for n in names(mcp) | names(admin_mcp) if n.startswith("_")},
            set())

    def test_every_tool_has_a_description(self):
        async def ask():
            async with Client(mcp) as client:
                return await client.list_tools()
        for tool in asyncio.run(ask()):
            self.assertTrue((tool.description or "").strip(),
                            f"{tool.name} has no docstring")


if __name__ == "__main__":
    unittest.main()
