# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Spend limits.

Zero means no limit, and that is the default — a limit nobody asked for would
stop a fleet mid-run. The rest is about the limit being honest: it brakes new
work rather than capping a running task, and it says which figure it is
measuring, since a money figure from a subscription credential is an estimate.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agentbridge import ledger
from agentbridge.service import budget, config
from agentbridge.service.tenant import tenant_from_headers


def stamp(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(
        timespec="seconds")


class BudgetBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for patch in (
            mock.patch.object(config, "TENANTS_DIR", root / "tenants"),
            mock.patch.object(config, "WORKSPACES_DIR", root / "ws"),
            # Must be redirected too: provisioning writes a login, and the
            # real file belongs to the service user.
            mock.patch.object(config, "HTPASSWD_FILE", root / "htpasswd" / "users"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config.TENANTS_DIR.mkdir()

    def tenant(self, **limits):
        record = {"vhost": "ab-alice", "bus_user": "ab-alice",
                  "bus_password": "p",
                  "limits": {"max_workers": 2, **limits}}
        (config.TENANTS_DIR / "alice.json").write_text(json.dumps(record))
        return tenant_from_headers({config.USER_HEADER: "alice"})

    def spend(self, tenant, cost=0.0, input_tokens=0, output_tokens=0,
              cache=0, **delta):
        ledger.append(tenant.ledger, {
            "kind": "task", "rc": 0, "ts_end": stamp(**delta),
            "ts_start": stamp(**delta), "cost_usd": cost,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_tokens": cache,
        })


class Defaults(BudgetBase):
    def test_no_limit_by_default(self):
        tenant = self.tenant()
        self.assertEqual(tenant.max_cost_usd, 0.0)
        self.assertEqual(tenant.max_tokens, 0)
        self.assertFalse(budget.status(tenant)["enforced"])

    def test_spending_a_fortune_is_allowed_when_unlimited(self):
        tenant = self.tenant()
        self.spend(tenant, cost=9999.0, input_tokens=10**9)
        # No exception: nothing was asked for, so nothing is enforced.
        self.assertEqual(budget.check(tenant)["cost_usd"], 9999.0)

    def test_the_default_window(self):
        self.assertEqual(self.tenant().budget_window, config.BUDGET_WINDOW)


class CostLimit(BudgetBase):
    def test_under_the_limit_passes(self):
        tenant = self.tenant(max_cost_usd=1.0)
        self.spend(tenant, cost=0.25)
        spent = budget.check(tenant)
        self.assertEqual(spent["cost_usd"], 0.25)
        self.assertEqual(spent["cost_usd_remaining"], 0.75)

    def test_at_the_limit_stops_new_work(self):
        tenant = self.tenant(max_cost_usd=1.0)
        self.spend(tenant, cost=1.0)
        with self.assertRaises(budget.BudgetExceeded) as cm:
            budget.check(tenant)
        message = str(cm.exception)
        # The message has to be actionable: the figures and the window.
        self.assertIn("1.0000", message)
        self.assertIn("30d", message)
        self.assertIn("No new tasks", message)

    def test_spending_accumulates_across_tasks(self):
        tenant = self.tenant(max_cost_usd=0.5)
        for _ in range(5):
            self.spend(tenant, cost=0.08)
        self.assertAlmostEqual(budget.check(tenant)["cost_usd"], 0.40)
        self.spend(tenant, cost=0.2)
        with self.assertRaises(budget.BudgetExceeded):
            budget.check(tenant)

    def test_spending_outside_the_window_does_not_count(self):
        tenant = self.tenant(max_cost_usd=1.0, budget_window="24h")
        self.spend(tenant, cost=5.0, days=3)
        self.assertEqual(budget.check(tenant)["cost_usd"], 0.0)

    def test_remaining_never_goes_negative(self):
        tenant = self.tenant(max_cost_usd=1.0)
        self.spend(tenant, cost=3.0)
        self.assertEqual(budget.status(tenant)["cost_usd_remaining"], 0.0)


class TokenLimit(BudgetBase):
    def test_input_and_output_count(self):
        tenant = self.tenant(max_tokens=1000)
        self.spend(tenant, input_tokens=300, output_tokens=200)
        self.assertEqual(budget.check(tenant)["tokens"], 500)

    def test_cache_reads_are_excluded(self):
        # A long preamble re-read from cache every task would otherwise
        # dominate a count it costs a tenth of the price to produce.
        tenant = self.tenant(max_tokens=1000)
        self.spend(tenant, input_tokens=10, output_tokens=10, cache=500_000)
        self.assertEqual(budget.check(tenant)["tokens"], 20)

    def test_at_the_limit_stops_new_work(self):
        tenant = self.tenant(max_tokens=100)
        self.spend(tenant, input_tokens=60, output_tokens=40)
        with self.assertRaises(budget.BudgetExceeded) as cm:
            budget.check(tenant)
        self.assertIn("100", str(cm.exception))
        self.assertIn("token limit", str(cm.exception))

    def test_either_limit_alone_is_enough_to_stop(self):
        tenant = self.tenant(max_cost_usd=10.0, max_tokens=50)
        self.spend(tenant, cost=0.01, input_tokens=100)
        with self.assertRaises(budget.BudgetExceeded) as cm:
            budget.check(tenant)
        self.assertIn("token", str(cm.exception))


class Honesty(BudgetBase):
    def test_a_truncated_ledger_window_is_declared(self):
        tenant = self.tenant(max_cost_usd=10**6)
        with mock.patch.object(ledger, "MAX_RECORDS", 3):
            for _ in range(5):
                self.spend(tenant, cost=0.01)
            spent = budget.check(tenant)
        self.assertTrue(spent["truncated"])

    def test_the_refusal_says_the_figure_is_a_floor_when_truncated(self):
        tenant = self.tenant(max_cost_usd=0.02)
        with mock.patch.object(ledger, "MAX_RECORDS", 3):
            for _ in range(5):
                self.spend(tenant, cost=0.01)
            with self.assertRaises(budget.BudgetExceeded) as cm:
                budget.check(tenant)
        self.assertIn("floor", str(cm.exception))

    def test_checks_do_not_count_against_the_budget(self):
        # A run_check costs nothing: no model, no credential.
        tenant = self.tenant(max_cost_usd=1.0)
        ledger.append(tenant.ledger, {
            "kind": "check", "rc": 0, "ts_end": stamp(), "duration_ms": 10})
        self.assertEqual(budget.check(tenant)["cost_usd"], 0.0)

    def test_a_row_with_no_cost_is_not_an_error(self):
        tenant = self.tenant(max_cost_usd=1.0)
        ledger.append(tenant.ledger, {"kind": "task", "rc": 0,
                                      "ts_end": stamp()})
        self.assertEqual(budget.check(tenant)["cost_usd"], 0.0)


class ProvisionReportsWhatItStored(BudgetBase):
    """The return value must not report defaults for fields it just set."""

    def test_limits_come_back_as_stored(self):
        from agentbridge.service import provisioning, rabbit_mgmt
        with mock.patch.object(rabbit_mgmt, "ensure_tenant"):
            created = provisioning.provision("alice", max_cost_usd=2.5,
                                             max_tokens=1000,
                                             budget_window="7d")
        self.assertEqual(created["max_cost_usd"], 2.5)
        self.assertEqual(created["max_tokens"], 1000)
        self.assertEqual(created["budget_window"], "7d")

    def test_updating_one_limit_reports_the_others_unchanged(self):
        from agentbridge.service import provisioning, rabbit_mgmt
        with mock.patch.object(rabbit_mgmt, "ensure_tenant"):
            provisioning.provision("alice", max_cost_usd=2.5, max_tokens=1000)
            updated = provisioning.provision("alice", max_cost_usd=5.0)
        self.assertEqual(updated["max_cost_usd"], 5.0)
        self.assertEqual(updated["max_tokens"], 1000)

    def test_editing_a_limit_does_not_revoke_admin(self):
        # The admin endpoint's provision_user defaults admin to None for this
        # reason: a default of False silently took admin away from anyone
        # whose record was edited for an unrelated reason.
        import asyncio

        from agentbridge.service import admin_api, provisioning, rabbit_mgmt
        with mock.patch.object(rabbit_mgmt, "ensure_tenant"):
            provisioning.provision("alice", admin=True)
            result = asyncio.run(admin_api.provision_user(
                "alice", max_cost_usd=1.0,
                headers={config.USER_HEADER: "alice"}))
        self.assertTrue(result["admin"], "admin was revoked as a side effect")
        self.assertEqual(result["max_cost_usd"], 1.0)


class Validation(unittest.TestCase):
    def test_zero_and_positive_are_fine(self):
        self.assertEqual(budget.validate_limit(0, "max_cost_usd"), 0.0)
        self.assertEqual(budget.validate_limit("2.5", "max_cost_usd"), 2.5)

    def test_negative_and_nonsense_are_refused(self):
        for bad in (-1, "-0.5"):
            with self.assertRaises(ValueError):
                budget.validate_limit(bad, "max_cost_usd")
        for bad in ("lots", None, {}):
            with self.assertRaises(ValueError):
                budget.validate_limit(bad, "max_cost_usd")


class Windows(unittest.TestCase):
    """A budget window is narrower than a ledger query, for two reasons."""

    def test_a_relative_span_is_accepted_and_trimmed(self):
        self.assertEqual(budget.validate_window(" 30d "), "30d")
        self.assertEqual(budget.validate_window("24h"), "24h")
        self.assertEqual(budget.validate_window("90m"), "90m")

    def test_a_fixed_date_is_refused_even_though_the_ledger_parses_it(self):
        # ledger.parse_since accepts it, so this is a deliberate narrowing:
        # a bound that never rolls forward is not a window, and once spend
        # crossed the line the limit would be permanent.
        self.assertIsNotNone(ledger.parse_since("2026-10-28"))
        with self.assertRaises(ValueError) as cm:
            budget.validate_window("2026-10-28")
        self.assertIn("never", str(cm.exception))

    def test_a_zero_length_window_is_refused(self):
        # The trap: 0d holds no tasks, so spend inside it is always 0 and the
        # limit can never trip — enforcement off while the record claims a
        # limit is on.
        with self.assertRaises(ValueError) as cm:
            budget.validate_window("0d")
        self.assertIn("ever be reached", str(cm.exception))

    def test_unparseable_spans_are_refused(self):
        for bad in ("30", "d", "30s", "", "week"):
            with self.assertRaises(ValueError):
                budget.validate_window(bad)


class SetLimits(BudgetBase):
    """`set_budget`'s worker: one record rewritten, nothing else touched."""

    def setUp(self):
        super().setUp()
        from agentbridge.service import provisioning, rabbit_mgmt
        self.provisioning = provisioning
        patch = mock.patch.object(rabbit_mgmt, "ensure_tenant")
        self.ensure = patch.start()
        self.addCleanup(patch.stop)
        provisioning.provision("alice", max_cost_usd=1.0, max_tokens=100)
        self.ensure.reset_mock()

    def test_it_changes_only_what_it_was_given(self):
        result = self.provisioning.set_limits("alice", max_tokens=5000)
        self.assertEqual(result["max_tokens"], 5000)
        self.assertEqual(result["max_cost_usd"], 1.0, "untouched field changed")
        self.assertEqual(result["budget_window"], "30d")

    def test_zero_removes_a_limit_rather_than_meaning_omitted(self):
        result = self.provisioning.set_limits("alice", max_cost_usd=0)
        self.assertEqual(result["max_cost_usd"], 0.0)
        # The token limit survives, so something is still enforced.
        self.assertTrue(result["usage"]["enforced"])
        result = self.provisioning.set_limits("alice", max_tokens=0)
        self.assertFalse(result["usage"]["enforced"])

    def test_it_does_not_touch_the_broker(self):
        # Not an optimisation: a budget change that failed because RabbitMQ
        # was unreachable would be a baffling error to read.
        self.provisioning.set_limits("alice", max_cost_usd=2.0)
        self.ensure.assert_not_called()

    def test_it_reports_usage_so_a_crossed_line_is_visible_at_once(self):
        tenant = tenant_from_headers({config.USER_HEADER: "alice"})
        self.spend(tenant, cost=0.5)
        result = self.provisioning.set_limits("alice", max_cost_usd=0.25)
        self.assertEqual(result["usage"]["cost_usd"], 0.5)
        self.assertEqual(result["usage"]["cost_usd_remaining"], 0.0)

    def test_setting_nothing_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            self.provisioning.set_limits("alice")
        self.assertIn("nothing to set", str(cm.exception))

    def test_an_unknown_tenant_is_refused(self):
        with self.assertRaises(ValueError):
            self.provisioning.set_limits("ghost", max_cost_usd=1.0)

    def test_a_bad_value_is_refused_before_the_record_is_rewritten(self):
        before = (config.TENANTS_DIR / "alice.json").read_text()
        for kw in ({"max_cost_usd": -5}, {"max_tokens": -1},
                   {"max_cost_usd": "lots"}, {"budget_window": "0d"},
                   {"budget_window": "2026-01-01"}):
            with self.assertRaises(ValueError):
                self.provisioning.set_limits("alice", **kw)
        self.assertEqual((config.TENANTS_DIR / "alice.json").read_text(), before)

    def test_provision_refuses_the_same_bad_values(self):
        # The path that existed first, and the one that could write a negative
        # limit straight into a record: validate_limit was called by nothing
        # but its own tests until set_budget wired it in.
        for kw in ({"max_cost_usd": -5}, {"max_tokens": -1},
                   {"budget_window": "banana"}):
            with self.assertRaises(ValueError):
                self.provisioning.provision("alice", **kw)

    def test_a_negative_limit_would_have_blocked_the_tenant_forever(self):
        # Why the guard matters: a negative limit is not a loose one.
        tenant = self.tenant(max_cost_usd=-5.0)
        with self.assertRaises(budget.BudgetExceeded):
            budget.check(tenant)


class SetBudgetTool(BudgetBase, unittest.IsolatedAsyncioTestCase):
    """The admin tool around it."""

    def setUp(self):
        super().setUp()
        from agentbridge.service import admin_api, provisioning, rabbit_mgmt, ratelimit
        self.admin_api = admin_api
        patch = mock.patch.object(rabbit_mgmt, "ensure_tenant")
        patch.start()
        self.addCleanup(patch.stop)
        provisioning.provision("alice", admin=True)
        provisioning.provision("bob")
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)

    async def test_an_admin_can_set_another_tenant_s_budget(self):
        result = await self.admin_api.set_budget(
            "bob", max_tokens=1_000_000,
            headers={config.USER_HEADER: "alice"})
        self.assertEqual(result["user"], "bob")
        self.assertEqual(result["max_tokens"], 1_000_000)
        self.assertTrue(result["usage"]["enforced"])

    async def test_a_plain_tenant_is_refused_and_nothing_changes(self):
        before = (config.TENANTS_DIR / "alice.json").read_text()
        result = await self.admin_api.set_budget(
            "alice", max_cost_usd=999.0,
            headers={config.USER_HEADER: "bob"})
        self.assertFalse(result["success"])
        self.assertIn("not an administrator", result["error"])
        self.assertEqual((config.TENANTS_DIR / "alice.json").read_text(), before)

    async def test_a_bad_window_comes_back_as_an_error_not_a_traceback(self):
        result = await self.admin_api.set_budget(
            "bob", budget_window="2026-01-01",
            headers={config.USER_HEADER: "alice"})
        self.assertFalse(result["success"])
        self.assertIn("window", result["error"])

    async def test_provision_user_also_returns_a_bad_window_as_an_error(self):
        result = await self.admin_api.provision_user(
            "bob", budget_window="banana",
            headers={config.USER_HEADER: "alice"})
        self.assertFalse(result["success"])


class Enforcement(BudgetBase, unittest.IsolatedAsyncioTestCase):
    """The tools that spend money refuse once the budget is gone."""

    async def test_send_task_is_refused(self):
        from agentbridge.service import broker, server
        tenant = self.tenant(max_cost_usd=0.01)
        self.spend(tenant, cost=0.5)
        (config.WORKSPACES_DIR / "alice").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(broker, "publish", new=mock.AsyncMock()) as pub:
            result = await server.send_task(
                to="researcher.work", body="x",
                headers={config.USER_HEADER: "alice"})
        self.assertFalse(result["success"])
        self.assertIn("cost limit reached", result["error"])
        pub.assert_not_called()

    async def test_broadcast_is_refused(self):
        from agentbridge.service import broker, server
        tenant = self.tenant(max_tokens=10)
        self.spend(tenant, input_tokens=100)
        with mock.patch.object(broker, "publish", new=mock.AsyncMock()) as pub:
            result = await server.broadcast(
                "x", headers={config.USER_HEADER: "alice"})
        self.assertFalse(result["success"])
        self.assertIn("token limit reached", result["error"])
        pub.assert_not_called()

    async def test_an_unlimited_tenant_is_not_blocked(self):
        from agentbridge.service import broker, server
        tenant = self.tenant()
        self.spend(tenant, cost=100.0)
        with mock.patch.object(broker, "publish", new=mock.AsyncMock()), \
             mock.patch.object(broker, "publish_dispatch", new=mock.AsyncMock()):
            result = await server.broadcast(
                "x", headers={config.USER_HEADER: "alice"})
        self.assertTrue(result["sent"])


if __name__ == "__main__":
    unittest.main()
