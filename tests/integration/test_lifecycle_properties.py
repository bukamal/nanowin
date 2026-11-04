"""Property-based invariants over random trade sequences.

The deterministic lifecycle test covers one realistic path; this one asks
Hypothesis for many short buy/sell sequences and asserts the same
*structural* invariants hold every time -- trial balance, inventory value
(dashboard) == inventory valuation (report), AR/AP == invoice totals, and
rebuild idempotency. It catches ordering-dependent accounting drift that a
single hand-written scenario can miss.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from nano_offline.app_context import AppContext
from nano_offline.services.invoice_service import InvoiceLineInput


def _new_ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-prop-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def _trial_balance(ctx: AppContext) -> tuple[float, float]:
    with ctx.db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c FROM ledger_entries"
        ).fetchone()
    return float(row["d"]), float(row["c"])


TRADES = st.lists(
    st.tuples(st.sampled_from(["buy", "sell"]), st.integers(1, 5), st.integers(5, 40)),
    min_size=1,
    max_size=6,
)


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(trades=TRADES)
def test_invariants_hold_over_random_trade_sequences(trades):
    ctx, tmp = _new_ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        category = ctx.definitions.create_category("مواد")
        customer = ctx.customers.create("عميل")
        supplier = ctx.suppliers.create("مورد")
        item_id = ctx.items.create(
            name="مادة", category_id=category, item_type="مخزون",
            purchase_price=5.0, selling_price=10.0, quantity=10.0,
            base_unit_id=unit, barcode="PROP-1",
        )
        ctx.invoices.convert_opening_balances(item_ids=[item_id], reference="أول المدة")

        expected_sales = 0.0
        expected_purchases = 0.0
        for kind, qty, price in trades:
            if kind == "buy":
                ctx.invoices.create_invoice(
                    invoice_type="purchase", supplier_id=supplier,
                    lines=[InvoiceLineInput(description="مادة", item_id=item_id, quantity=qty, unit_price=float(price), unit_id=unit)],
                )
                expected_purchases += qty * price
            else:
                ctx.invoices.create_invoice(
                    invoice_type="sale", customer_id=customer, allow_negative_stock=True,
                    lines=[InvoiceLineInput(description="مادة", item_id=item_id, quantity=qty, unit_price=float(price), unit_id=unit)],
                )
                expected_sales += qty * price

        # 1) every posting is balanced
        debit, credit = _trial_balance(ctx)
        assert debit == pytest.approx(credit, abs=1e-6)

        # 2) the two inventory valuations agree
        summary_value = ctx.dashboard.summary()["inventory_value"]
        report_value = ctx.reports.inventory_valuation()["total_value"]
        assert report_value == pytest.approx(summary_value, abs=1e-6)

        # 3) no payments were made -> AR/AP are exactly the invoice totals
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(expected_sales, abs=1e-6)
        assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(expected_purchases, abs=1e-6)

        # 4) rebuild is a no-op (no drift)
        ctx.invoices.rebuild_derived_state()
        assert ctx.dashboard.summary()["inventory_value"] == pytest.approx(summary_value, abs=1e-6)
        assert ctx.reports.inventory_valuation()["total_value"] == pytest.approx(report_value, abs=1e-6)
        debit2, credit2 = _trial_balance(ctx)
        assert debit2 == pytest.approx(credit2, abs=1e-6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)