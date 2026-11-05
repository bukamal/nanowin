"""Accounting/validation edge cases beyond the happy-path lifecycle.

Focuses on the guards and corner cases the full-cycle tests do not exercise:
rejecting overselling without corrupting state, allowing negative stock while
keeping the invariants, voucher validation, secondary-unit conversion on an
invoice line, decimal-precision settings, and USD-era invoice snapshots.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import currency, precision_settings
from nano_offline.services.invoice_service import InvoiceLineInput


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-edge-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def _trial(ctx: AppContext) -> tuple[float, float]:
    with ctx.db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c FROM ledger_entries"
        ).fetchone()
    return float(row["d"]), float(row["c"])


def _stock_item(ctx, qty: float, purchase: float = 10.0, selling: float = 15.0) -> int:
    unit = ctx.definitions.create_unit("قطعة", "ق")
    item = ctx.items.create(
        name="مادة", purchase_price=purchase, selling_price=selling,
        quantity=qty, base_unit_id=unit, barcode="EDGE-1",
    )
    ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
    return item


def test_overselling_without_permission_is_rejected_and_state_untouched():
    ctx, tmp = _ctx()
    try:
        customer = ctx.customers.create("عميل")
        item = _stock_item(ctx, 5.0)
        before = len(ctx.invoices.list_invoices())

        with pytest.raises(ValueError, match="المخزون غير كافٍ"):
            ctx.invoices.create_invoice(
                invoice_type="sale", customer_id=customer,
                lines=[InvoiceLineInput(description="x", item_id=item, quantity=10, unit_price=15.0)],
            )

        assert ctx.items.get(item)["quantity"] == pytest.approx(5.0)
        assert len(ctx.invoices.list_invoices()) == before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_negative_stock_allowed_keeps_invariants():
    ctx, tmp = _ctx()
    try:
        customer = ctx.customers.create("عميل")
        item = _stock_item(ctx, 5.0)
        avg = float(ctx.items.get(item)["average_cost"])

        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, allow_negative_stock=True,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=8, unit_price=15.0)],
        )
        item_row = ctx.items.get(item)
        assert item_row["quantity"] == pytest.approx(-3.0)
        assert ctx.reports.inventory_valuation()["total_value"] == pytest.approx(-3.0 * avg, abs=1e-6)
        assert ctx.reports.income_statement()["cogs"] == pytest.approx(8 * avg, abs=1e-6)
        d, c = _trial(ctx)
        assert d == pytest.approx(c, abs=1e-6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_voucher_amount_must_be_positive():
    ctx, tmp = _ctx()
    try:
        customer = ctx.customers.create("عميل")
        for bad in (0.0, -5.0):
            with pytest.raises(ValueError):
                ctx.payments.create_voucher(voucher_type="receipt", amount=bad, customer_id=customer)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_purchase_by_secondary_unit_records_base_quantity_and_total():
    ctx, tmp = _ctx()
    try:
        piece = ctx.definitions.create_unit("قطعة", "ق")
        carton = ctx.definitions.create_unit("كرتونة", "ك")
        supplier = ctx.suppliers.create("مورد")
        item = ctx.items.create(
            name="مادة", purchase_price=10.0, selling_price=15.0, quantity=0.0,
            base_unit_id=piece, barcode="EDGE-2",
            item_units=[{"unit_id": carton, "conversion_factor": 12}],
        )
        ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=2, unit_price=100.0, unit_id=carton)],
        )
        assert ctx.items.get(item)["quantity"] == pytest.approx(24.0)
        with ctx.db.connect() as conn:
            line = conn.execute(
                "SELECT quantity,total,quantity_in_base FROM invoice_lines ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert float(line["quantity"]) == pytest.approx(2.0)
        assert float(line["total"]) == pytest.approx(200.0)
        assert float(line["quantity_in_base"]) == pytest.approx(24.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_precision_settings_change_display_not_storage():
    ctx, tmp = _ctx()
    try:
        ctx.settings.set_many({
            precision_settings.SETTING_PRICE: "3",
            precision_settings.SETTING_QTY: "2",
            precision_settings.SETTING_TOTAL: "0",
        })
        currency.set_display_currency(ctx.settings, "USD")

        assert currency.format_amount(1.5, ctx.settings, field="price") == "1.500 $"
        assert currency.format_qty(2.0, ctx.settings) == "2.00"
        assert currency.format_amount(126.0, ctx.settings) == "126 $"

        # Stored money is a plain float regardless of the display precision.
        customer = ctx.customers.create("عميل")
        item = _stock_item(ctx, 4.0)
        inv = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=2, unit_price=15.0)],
        )
        assert ctx.invoices.get_invoice(inv)["total"] == pytest.approx(30.0, abs=1e-9)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_usd_era_invoice_snapshot_survives_switching_back_to_syp():
    ctx, tmp = _ctx()
    try:
        currency.set_display_currency(ctx.settings, "USD")
        customer = ctx.customers.create("عميل")
        item = _stock_item(ctx, 4.0)
        inv = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=1, unit_price=15.0)],
        )
        assert ctx.invoices.get_invoice(inv)["invoice_currency_code"] == "USD"

        currency.set_display_currency(ctx.settings, "SYP")
        assert ctx.invoices.get_invoice(inv)["total"] == pytest.approx(15.0, abs=1e-9)
        html = ctx.documents.invoice_html(inv)
        assert "$" in html
    finally:
        shutil.rmtree(tmp, ignore_errors=True)