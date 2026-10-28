# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The fleet view: what every worker is doing, merged from two sources.

The bridge's status file says what the worker thinks it is doing; systemd says
whether the worker is still there at all. Neither alone is enough — a killed
worker leaves a file claiming ``working`` forever, and systemd knows a unit is
active without knowing which task it took — so this merges them and lets
systemd win on the question of existence.
"""

from __future__ import annotations

import asyncio
import logging

from .. import ledger, status
from . import checks, config, rabbit_mgmt
from .tenant import Tenant

log = logging.getLogger("mcp-agentbridge")

# Unit states the bridge cannot report, because a worker in them writes
# nothing: the service supplies them from systemd.
GONE = "gone"
FAILED = "failed"


def status_path(tenant: Tenant, agent_id: str):
    return tenant.status_dir / f"{agent_id}.json"


def merge(statuses: list[dict], units: list[dict]) -> list[dict]:
    """One row per worker, from the union of both sources.

    A unit with no status file is still shown (it may be mid-start), and a
    status file whose unit has gone is shown as ``gone`` rather than dropped,
    so a worker that died while working is visible instead of silently absent.
    """
    by_id = {s.get("agent"): dict(s) for s in statuses if s.get("agent")}
    rows = []
    seen = set()

    for unit in units:
        agent_id = unit["id"]
        seen.add(agent_id)
        row = by_id.get(agent_id, {"agent": agent_id})
        row["unit"] = unit.get("unit")
        row["unit_active"] = unit.get("active")
        row["unit_sub"] = unit.get("sub")
        if unit.get("active") == "failed" or unit.get("sub") == "failed":
            row["state"] = FAILED
        elif unit.get("active") not in ("active", "activating"):
            row["state"] = GONE
        row.setdefault("state", "starting")
        rows.append(row)

    # A status file with no unit at all: the unit is gone and the file is
    # stale. Report it once, then `sweep` removes it.
    for agent_id, row in by_id.items():
        if agent_id not in seen:
            row["state"] = GONE
            row["unit_active"] = None
            rows.append(row)

    rows.sort(key=lambda r: str(r.get("agent")))
    return rows


def sweep(tenant: Tenant, units: list[dict]) -> list[str]:
    """Delete status files whose unit no longer exists. Returns what went."""
    live = {u["id"] for u in units}
    removed = []
    for record in status.read_dir(tenant.status_dir):
        agent_id = record.get("agent")
        if agent_id and agent_id not in live:
            try:
                status_path(tenant, agent_id).unlink()
                removed.append(agent_id)
            except OSError:
                pass
    return removed


def _totals(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    return counts


def _usage_7d(tenant: Tenant) -> dict:
    recent = ledger.records(tenant.ledger, limit=ledger.MAX_RECORDS, since="7d")
    summary = ledger.summarize(recent, by="model")
    return {
        "tasks": summary["totals"]["tasks"],
        "cost_usd": summary["totals"]["cost_usd"],
        "cost_estimated": summary["cost_estimated"],
    }


async def queue_depths(tenant: Tenant) -> list[dict] | None:
    """The tenant's queues, or None when the management API is not configured.

    Depths are useful but not essential, so a broken management API degrades
    the view instead of failing the call.
    """
    if not config.RABBITMQ_ADMIN_USER:
        return None
    try:
        rows = await asyncio.to_thread(rabbit_mgmt.queues, tenant.vhost)
    except rabbit_mgmt.RabbitError as exc:
        log.info("user=%s fleet_status queues unavailable: %s", tenant.name, exc)
        return None
    return rows


async def snapshot(tenant: Tenant, units: list[dict]) -> dict:
    """The whole fleet view: workers, queues, totals. Metadata only."""
    statuses = await asyncio.to_thread(status.read_dir, tenant.status_dir)
    rows = merge(statuses, units)
    await asyncio.to_thread(sweep, tenant, units)
    queues, usage = await asyncio.gather(
        queue_depths(tenant),
        asyncio.to_thread(_usage_7d, tenant),
    )
    # Checks share the unit prefix and the worker budget, but they are not
    # workers you can send a task to, so they are reported separately.
    check_rows = [r for r in rows if checks.is_check(str(r.get("agent", "")))]
    worker_rows = [r for r in rows if r not in check_rows]
    result = {
        "user": tenant.name,
        "workers": worker_rows,
        "checks_running": [
            {"check_id": str(r["agent"]).removeprefix(checks.CHECK_PREFIX),
             "state": r.get("state"), "unit_active": r.get("unit_active")}
            for r in check_rows
        ],
        "totals": {
            "workers": len(worker_rows),
            "checks": len(check_rows),
            "by_state": _totals(worker_rows),
            # Workers and checks draw on one budget, so say so here rather
            # than letting a caller add up the wrong things.
            "max_workers": tenant.max_workers,
            "budget_used": len([r for r in rows
                                if r.get("unit_active") in ("active", "activating")]),
            "usage_7d": usage,
        },
    }
    if queues is not None:
        result["queues"] = queues
    return result
