"""Editing/deleting invoices and vouchers must fully reverse their effects.

These are the highest-risk accounting operations (they rewrite history in one
transaction and re-derive inventory, ledger, balances and paid amounts). The
test asserts the reversal is exact and that a subsequent rebuild is a no-op.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.services.invoice_service import InvoiceLineInput

PASSWORD = "StrongPass1"


def _new_ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-rev-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.auth.login("admin", PASSWORD)
    return ctx, tmp


def _trial(ctx: AppContext) -> tuple[float, float]:
    with ctx.db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c FROM ledger_entries"
        ).fetchone()
    return float(row["d"]), float(row["c"])


def _line(item_id: int, unit_id: int, qty: float, price: float) -> InvoiceLineInput:
    return InvoiceLineInput(description="مادة", item_id=item_id, quantity=qty, unit_price=price, unit_id=unit_id)


def test_editing_and_deleting_an_invoice_reverses_stock_and_accounting():
    ctx, tmp = _new_ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        category = ctx.definitions.create_category("مواد")
        customer = ctx.customers.create("عميل")
        supplier = ctx.suppliers.create("مورد")
        item = ctx.items.create(
            name="مادة", category_id=category, purchase_price=5.0, selling_price=10.0,
            quantity=10.0, base_unit_id=unit, barcode="REV-1",
        )
        ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
        ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier,
            lines=[_line(item, unit, 5, 6.0)],
        )
        sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[_line(item, unit, 4, 10.0)],
        )
        avg = float(ctx.items.get(item)["average_cost"])

        # Baseline: 10 + 5 - 4 = 11 units; AR 40; AP 30.
        assert ctx.items.get(item)["quantity"] == pytest.approx(11.0)
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(40.0)
        assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(30.0)
        assert ctx.reports.income_statement()["sales"] == pytest.approx(40.0)
        assert ctx.reports.income_statement()["cogs"] == pytest.approx(4 * avg)
        d, c = _trial(ctx)
        assert d == pytest.approx(c, abs=1e-6)

        # Edit: 4 @ 10 -> 2 @ 12 (total 24). Stock rises by 2.
        ctx.invoices.update_invoice(
            sale, invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[_line(item, unit, 2, 12.0)],
        )
        assert ctx.items.get(item)["quantity"] == pytest.approx(13.0)
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(24.0)
        assert ctx.reports.income_statement()["sales"] == pytest.approx(24.0)
        assert ctx.reports.income_statement()["cogs"] == pytest.approx(2 * avg)
        d, c = _trial(ctx)
        assert d == pytest.approx(c, abs=1e-6)

        # Delete: everything reverses to the post-purchase state.
        ctx.invoices.delete_invoice(sale)
        assert ctx.items.get(item)["quantity"] == pytest.approx(15.0)
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(0.0)
        assert ctx.reports.income_statement()["sales"] == pytest.approx(0.0)
        assert ctx.reports.income_statement()["cogs"] == pytest.approx(0.0)
        assert ctx.reports.inventory_valuation()["total_value"] == pytest.approx(15.0 * avg, abs=1e-6)
        d, c = _trial(ctx)
        assert d == pytest.approx(c, abs=1e-6)

        # A rebuild after all that is a no-op.
        before = (ctx.dashboard.summary()["inventory_value"], ctx.reports.cash_movement()["closing_balance"])
        ctx.invoices.rebuild_derived_state()
        assert ctx.dashboard.summary()["inventory_value"] == pytest.approx(before[0], abs=1e-6)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(before[1], abs=1e-6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_editing_and_deleting_a_voucher_reverses_ar_and_cash():
    ctx, tmp = _new_ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        customer = ctx.customers.create("عميل")
        item = ctx.items.create(
            name="مادة", purchase_price=5.0, selling_price=10.0,
            quantity=10.0, base_unit_id=unit, barcode="REV-2",
        )
        ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
        sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[_line(item, unit, 3, 10.0)],
        )

        voucher = ctx.payments.create_voucher(
            voucher_type="receipt", amount=20.0, customer_id=customer,
            allocation_mode="manual", allocations={sale: 20.0},
        )
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(10.0)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(20.0)

        ctx.payments.update_voucher(
            voucher, voucher_type="receipt", amount=5.0, customer_id=customer,
            allocation_mode="manual", allocations={sale: 5.0},
        )
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(25.0)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(5.0)

        ctx.payments.delete_voucher(voucher)
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(30.0)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(0.0)
        d, c = _trial(ctx)
        assert d == pytest.approx(c, abs=1e-6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)