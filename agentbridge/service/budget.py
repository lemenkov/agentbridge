# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Spend limits: stop starting work once a tenant has used its budget.

Two limits, both **off by default** (zero means no limit), both per tenant and
both measured over a rolling window from the task ledger (B.3):

* ``max_cost_usd`` — what the model reported the work cost.
* ``max_tokens`` — input plus output tokens, cache reads excluded.

**This is a brake, not a cap, and the distinction matters.** The check happens
before a task is dispatched, so the last task admitted can carry the total past
the limit — by its own cost, which nothing can know in advance. A tenant with
0.05 left can start a task that spends a dollar. What the limit guarantees is
that no *new* work starts once the line is crossed.

**On the money figure.** ``cost_usd`` is the CLI's own number. With an API key
it tracks what is billed; with a subscription credential (``apiKeySource`` of
``none``) it is a notional estimate of what the same work would have cost, so a
money limit on such a credential limits an estimate. The token limit measures
something real either way, which is why both exist.

**Cache reads are excluded from the token total** on purpose. A long preamble
re-read from cache on every task would otherwise dominate the count while
costing a tenth of the price, and a limit that is mostly measuring cache hits
would mean nothing.
"""

from __future__ import annotations

import logging
import re

from .. import ledger
from .tenant import Tenant

log = logging.getLogger("mcp-agentbridge")

COUNTED_TOKEN_FIELDS = ("input_tokens", "output_tokens")

# A budget window is deliberately narrower than a ledger query: relative only,
# and at least one unit long. See `validate_window`.
_WINDOW = re.compile(r"^(\d+)([dhm])$")


class BudgetExceeded(Exception):
    """The tenant is out of budget (reported to the caller, not raised at them)."""


def validate_window(spec: str) -> str:
    """Return a usable budget window, or raise ``ValueError`` saying why not.

    **Validate on the way in, because the damage is on the way out.** A bad
    window does not break the budget report — it breaks *dispatch*. ``check``
    runs before every task, so a window the ledger cannot parse turns into a
    refusal on every ``send_task`` for that tenant. The moment to refuse it is
    the moment someone sets it, when there is a human to tell.

    Two things are rejected that ``ledger.parse_since`` accepts:

    * **An absolute date.** It parses, but it is not a window: the bound never
      rolls forward, so spend accumulates against it until the end of time and
      the limit becomes permanent once reached.
    * **Zero.** ``0d`` is a window containing no tasks, so the spend inside it
      is always 0 and the limit can never trip. That is enforcement switched
      off while the record still claims a limit — worse than no limit at all,
      which at least reports ``enforced: false``.
    """
    text = str(spec).strip()
    m = _WINDOW.match(text)
    if not m:
        raise ValueError(
            f"invalid budget window {spec!r}: use a relative span such as 30d, "
            f"24h or 90m. A fixed date is not a window — its bound would never "
            f"roll forward."
        )
    if int(m.group(1)) < 1:
        raise ValueError(
            f"invalid budget window {spec!r}: a zero-length window holds no "
            f"tasks, so no limit measured over it could ever be reached."
        )
    # Belt and braces: the ledger is what actually has to parse this.
    ledger.parse_since(text)
    return text


def usage(tenant: Tenant) -> dict:
    """What this tenant has spent inside its budget window.

    ``truncated`` is true when the ledger window could not show the whole
    period, which makes the figures a floor rather than a total.
    """
    rows = ledger.records(
        tenant.ledger, limit=ledger.MAX_RECORDS, since=tenant.budget_window)
    cost = 0.0
    tokens = 0
    for row in rows:
        value = row.get("cost_usd")
        if isinstance(value, (int, float)):
            cost += value
        for field in COUNTED_TOKEN_FIELDS:
            count = row.get(field)
            if isinstance(count, (int, float)):
                tokens += int(count)
    return {
        "window": tenant.budget_window,
        "cost_usd": round(cost, 6),
        "tokens": tokens,
        "max_cost_usd": tenant.max_cost_usd,
        "max_tokens": tenant.max_tokens,
        "truncated": len(rows) >= ledger.MAX_RECORDS,
    }


def status(tenant: Tenant) -> dict:
    """``usage`` plus what is left and whether anything is enforced."""
    spent = usage(tenant)
    limited = bool(tenant.max_cost_usd or tenant.max_tokens)
    spent["enforced"] = limited
    if tenant.max_cost_usd:
        spent["cost_usd_remaining"] = round(
            max(0.0, tenant.max_cost_usd - spent["cost_usd"]), 6)
    if tenant.max_tokens:
        spent["tokens_remaining"] = max(0, tenant.max_tokens - spent["tokens"])
    return spent


def check(tenant: Tenant) -> dict:
    """Raise ``BudgetExceeded`` if no new work should start. Returns the usage.

    The message names the window and the figures, because "budget exceeded"
    without them tells the caller nothing they can act on.
    """
    spent = status(tenant)
    if not spent["enforced"]:
        return spent

    hint = (" The ledger window could not cover the whole period, so this is "
            "a floor." if spent["truncated"] else "")
    if tenant.max_cost_usd and spent["cost_usd"] >= tenant.max_cost_usd:
        raise BudgetExceeded(
            f"cost limit reached: ${spent['cost_usd']:.4f} of "
            f"${tenant.max_cost_usd:.4f} in the last {spent['window']}. No new "
            f"tasks will start until the window rolls forward or the operator "
            f"raises the limit.{hint}"
        )
    if tenant.max_tokens and spent["tokens"] >= tenant.max_tokens:
        raise BudgetExceeded(
            f"token limit reached: {spent['tokens']} of {tenant.max_tokens} "
            f"in the last {spent['window']}. No new tasks will start until the "
            f"window rolls forward or the operator raises the limit.{hint}"
        )
    return spent


def validate_limit(value, name: str) -> float:
    """A limit is a non-negative number; zero means no limit."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number (0 for no limit)") from None
    if number < 0:
        raise ValueError(f"{name} must not be negative (0 for no limit)")
    return number
