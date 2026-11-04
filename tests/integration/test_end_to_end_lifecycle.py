"""End-to-end business lifecycle on a real (temporary) SQLite database.

One test drives the whole trading cycle a shop actually performs and then
asserts the *cross-module* invariants that no single unit test can:
inventory valuation, the accounting equation (trial balance), cash, AR/AP,
the income statement, and rebuild idempotency.

Kept fixture-free (it makes and removes its own temp dir) so it runs both
under pytest and under the standalone runner used by the quality gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import currency
from nano_offline.services.invoice_service import InvoiceLineInput


def _new_ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-e2e-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def _ledger_totals(ctx: AppContext) -> tuple[float, float]:
    with ctx.db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c FROM ledger_entries"
        ).fetchone()
    return float(row["d"]), float(row["c"])


def _inventory_items_value(ctx: AppContext) -> float:
    total = 0.0
    for item in ctx.items.list():
        total += float(item.get("quantity") or 0) * float(item.get("average_cost") or 0)
    return total


def test_end_to_end_business_lifecycle():
    ctx, tmp = _new_ctx()
    try:
        # --- 1. admin configuration (currency + precision + display) ------- #
        currency.set_display_currency(ctx.settings, "SYP")
        currency.set_exchange_rate(ctx.settings, 1000)

        # --- 2. master data ------------------------------------------------ #
        unit = ctx.definitions.create_unit("قطعة", "ق")
        box = ctx.definitions.create_unit("كرتونة", "ك")
        category = ctx.definitions.create_category("مواد غذائية")
        customer = ctx.customers.create("عميل آجل")
        supplier = ctx.suppliers.create("مورد رئيسي")

        item_id = ctx.items.create(
            name="رز فاخر", category_id=category, item_type="مخزون",
            purchase_price=10.0, selling_price=15.0, quantity=12.0,
            base_unit_id=unit, barcode="SKU-RICE-1",
            item_units=[{"unit_id": box, "conversion_factor": 12, "purchase_price": 120.0, "selling_price": 180.0}],
        )
        service_id = ctx.items.create(name="خدمة توصيل", item_type="خدمة", selling_price=5.0)

        # --- 3. opening stock becomes an inventory-only purchase invoice --- #
        opening_id = ctx.invoices.convert_opening_balances(item_ids=[item_id], reference="أول المدة")
        assert opening_id is not None

        # --- 4. purchase on credit (no supplier payment yet) --------------- #
        ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier, paid_amount=0,
            lines=[InvoiceLineInput(description="رز فاخر", item_id=item_id, quantity=8, unit_price=12.0, unit_id=unit)],
        )

        # --- 5. sale with a partial down-payment --------------------------- #
        sale_id = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=25.0,
            lines=[
                InvoiceLineInput(description="رز فاخر", item_id=item_id, quantity=5, unit_price=15.0, unit_id=unit),
                InvoiceLineInput(description="خدمة توصيل", item_id=service_id, quantity=1, unit_price=5.0),
            ],
        )

        # --- 6. quick (POS) cash sale, fully paid, no party ---------------- #
        quick_id = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=None, paid_amount=45.0,
            lines=[InvoiceLineInput(description="رز فاخر", item_id=item_id, quantity=3, unit_price=15.0, unit_id=unit)],
        )

        # --- 7. partial payments: customer receipt + supplier payment ------ #
        ctx.payments.create_voucher(
            voucher_type="receipt", amount=20.0, customer_id=customer,
            allocation_mode="manual", allocations={sale_id: 20.0},
        )
        ctx.payments.create_voucher(voucher_type="payment", amount=40.0, supplier_id=supplier)

        # --- 8. cash expense ---------------------------------------------- #
        ctx.expenses.create_expense(amount=10.0, description="كهرباء")

        # --- 9. stocktake: system 12, counted 8 -> shrink 4 ---------------- #
        session = ctx.stocktake.start_session(notes="جرد دوري")
        scanned = ctx.stocktake.scan(session, "SKU-RICE-1")
        assert scanned["status"] == "added"
        ctx.stocktake.set_counted_qty(session, item_id, 8.0)
        assert ctx.stocktake.commit(session) == 1

        # ---------------- invariants --------------------------------------- #
        item = ctx.items.get(item_id)
        assert item["quantity"] == pytest.approx(8.0)
        assert item["average_cost"] == pytest.approx(10.8, abs=1e-6)

        # accounting equation: every posting is balanced
        debit, credit = _ledger_totals(ctx)
        assert debit == pytest.approx(credit, abs=1e-6)

        # inventory valuation equals sum(quantity * average_cost)
        valuation = ctx.reports.inventory_valuation()
        assert valuation["total_value"] == pytest.approx(_inventory_items_value(ctx), abs=1e-6)
        assert valuation["total_value"] == pytest.approx(8.0 * 10.8, abs=1e-6)

        # cash = quick sale 45 + sale down-payment 25 + receipt 20 - payment 40 - expense 10
        cash = ctx.reports.cash_movement()
        assert cash["closing_balance"] == pytest.approx(40.0, abs=1e-6)

        # AR = sale total (75+5=80) - down 25 - receipt 20 = 35
        # AP = purchase total 96 - paid 40 = 56
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(35.0, abs=1e-6)
        assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(56.0, abs=1e-6)

        # invoice-level paid amounts reconcile with the vouchers
        sale = ctx.invoices.get_invoice(sale_id)
        assert sale["total"] == pytest.approx(80.0, abs=1e-6)
        assert sale["paid_amount"] == pytest.approx(45.0, abs=1e-6)

        # income statement: sales 80 + 45 = 125; COGS = 8 * 10.8 = 86.4
        income = ctx.reports.income_statement()
        assert income["sales"] == pytest.approx(125.0, abs=1e-6)
        assert income["cogs"] == pytest.approx(86.4, abs=1e-6)
        assert income["gross_profit"] == pytest.approx(38.6, abs=1e-6)
        assert income["expenses"] == pytest.approx(10.0, abs=1e-6)
        assert income["net_profit"] == pytest.approx(28.6, abs=1e-6)

        # --- 10. rebuild is idempotent (no drift) -------------------------- #
        before = (cash["closing_balance"], valuation["total_value"], debit)
        ctx.invoices.rebuild_derived_state()
        ctx.expenses.rebuild_ledger()
        debit2, credit2 = _ledger_totals(ctx)
        assert debit2 == pytest.approx(credit2, abs=1e-6)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(before[0], abs=1e-6)
        assert ctx.reports.inventory_valuation()["total_value"] == pytest.approx(before[1], abs=1e-6)

        # --- 11. currency display never changes stored money --------------- #
        stored_total = ctx.invoices.get_invoice(quick_id)["total"]
        currency.set_display_currency(ctx.settings, "USD")
        assert ctx.invoices.get_invoice(quick_id)["total"] == pytest.approx(stored_total, abs=1e-9)
        assert currency.format_amount(stored_total, ctx.settings) == "45.00 $"
        currency.set_display_currency(ctx.settings, "SYP")
        assert ctx.invoices.get_invoice(quick_id)["total"] == pytest.approx(stored_total, abs=1e-9)
        assert currency.format_amount(stored_total, ctx.settings) == "45,000 ل.س"

        # --- 12. document renders with the configured currency ------------- #
        html = ctx.documents.invoice_html(sale_id)
        assert html and ("ل.س" in html or "$" in html)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
