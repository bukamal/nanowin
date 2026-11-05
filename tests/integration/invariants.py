"""Shared accounting invariants, in one place.

These are the rules every trade-*cycle* test asserts. Keeping them here (one
definition, reused) means a new workflow test only has to call
``assert_all(ctx)`` / ``assert_rebuild_idempotent(ctx)`` instead of
re-deriving balances by hand -- and the rules read as documentation:

* **Trial balance** -- every ledger posting is balanced (Σ debit = Σ credit).
* **Inventory value** -- the dashboard's ``quantity × average_cost`` equals the
  inventory report's valuation.
* **Cash** -- the CASH ledger total equals the dashboard's cash figure.
* **Allocations** -- no payment allocates more than its own amount.

Pure Python + the ``ctx`` services; no flet. Import path is added by callers
so it works under pytest and the standalone runner alike.
"""

from __future__ import annotations

import pytest


def trial_balance(ctx) -> tuple[float, float]:
    with ctx.db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c FROM ledger_entries"
        ).fetchone()
    return float(row["d"]), float(row["c"])


def assert_trial_balance(ctx, *, abs: float = 1e-6) -> None:
    debit, credit = trial_balance(ctx)
    assert debit == pytest.approx(credit, abs=abs), f"ledger unbalanced: {debit} != {credit}"


def inventory_value_from_items(ctx) -> float:
    return sum(
        float(item.get("quantity") or 0) * float(item.get("average_cost") or 0)
        for item in ctx.items.list()
        if item.get("item_type") == "مخزون"
    )


def inventory_value_from_report(ctx) -> float:
    return float(ctx.reports.inventory_valuation()["total_value"])


def assert_inventory_consistent(ctx, *, abs: float = 1e-6) -> None:
    report = inventory_value_from_report(ctx)
    items = inventory_value_from_items(ctx)
    assert report == pytest.approx(items, abs=abs), f"inventory report {report} != items {items}"


def assert_cash_matches_dashboard(ctx, *, abs: float = 1e-6) -> None:
    report = float(ctx.reports.cash_movement()["closing_balance"])
    dashboard = float(ctx.dashboard.summary()["cash"])
    assert report == pytest.approx(dashboard, abs=abs), f"cash report {report} != dashboard {dashboard}"


def assert_no_over_allocated_payments(ctx, *, abs: float = 1e-6) -> None:
    with ctx.db.connect() as conn:
        rows = conn.execute(
            """SELECT p.id AS id, p.amount AS amount, COALESCE(SUM(a.amount),0) AS allocated
               FROM payments p LEFT JOIN payment_allocations a ON a.payment_id = p.id
               GROUP BY p.id"""
        ).fetchall()
    for row in rows:
        assert float(row["allocated"]) <= float(row["amount"]) + abs, f"payment #{row['id']} over-allocated"


def _snapshot(ctx) -> dict:
    summary = ctx.dashboard.summary()
    debit, credit = trial_balance(ctx)
    return {
        "inventory": inventory_value_from_items(ctx),
        "cash": float(summary["cash"]),
        "sales": float(summary["sales"]),
        "debit": debit,
        "credit": credit,
    }


def assert_rebuild_idempotent(ctx, *, abs: float = 1e-6) -> None:
    before = _snapshot(ctx)
    ctx.invoices.rebuild_derived_state()
    ctx.expenses.rebuild_ledger()
    after = _snapshot(ctx)
    for key in ("inventory", "cash", "sales", "debit", "credit"):
        assert after[key] == pytest.approx(before[key], abs=abs), f"rebuild changed {key}: {before[key]} -> {after[key]}"


def assert_all(ctx, *, abs: float = 1e-6) -> None:
    assert_trial_balance(ctx, abs=abs)
    assert_inventory_consistent(ctx, abs=abs)
    assert_cash_matches_dashboard(ctx, abs=abs)
    assert_no_over_allocated_payments(ctx, abs=abs)


__all__ = [
    "trial_balance",
    "assert_trial_balance",
    "inventory_value_from_items",
    "inventory_value_from_report",
    "assert_inventory_consistent",
    "assert_cash_matches_dashboard",
    "assert_no_over_allocated_payments",
    "assert_rebuild_idempotent",
    "assert_all",
]