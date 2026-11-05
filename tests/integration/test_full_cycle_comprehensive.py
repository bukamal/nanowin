"""Comprehensive end-to-end cycle across every major module.

Covers, in one continuous business flow: admin/currency config, units (base +
secondary), categories, customer/supplier management, opening quantities,
credit purchase by the secondary unit, a mixed stock+service sale, a POS cash
sale, receipt/payment vouchers, an expense, the financial reports and the
currency snapshot behaviour -- then asserts the cross-module invariants.

Fixture-free (builds and removes its own temp dir) so it runs under pytest
and the standalone runner alike.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import currency
from nano_offline.services.invoice_service import InvoiceLineInput

PASSWORD = "StrongPass1"


def _new_ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-full-")
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


def _line(item_id: int, qty: float, price: float, unit_id: int | None = None, desc: str = "بند") -> InvoiceLineInput:
    return InvoiceLineInput(description=desc, item_id=item_id, quantity=qty, unit_price=price, unit_id=unit_id)


def test_comprehensive_project_cycle():
    ctx, tmp = _new_ctx()
    try:
        # --- admin / currency config --------------------------------------- #
        currency.set_display_currency(ctx.settings, "SYP")
        currency.set_exchange_rate(ctx.settings, 1000)

        # --- units + category + parties ------------------------------------ #
        piece = ctx.definitions.create_unit("قطعة", "ق")
        carton = ctx.definitions.create_unit("كرتونة", "ك")
        category = ctx.definitions.create_category("مواد غذائية")
        customer = ctx.customers.create("عميل أ", "0900")
        supplier = ctx.suppliers.create("مورد أ", "0911")
        assert {u["name"] for u in ctx.definitions.list_units()} >= {"قطعة", "كرتونة"}

        # --- customer / supplier management -------------------------------- #
        ctx.customers.update(customer, "عميل أ (محدّث)", "0999")
        assert ctx.customers.get(customer)["name"] == "عميل أ (محدّث)"
        temp = ctx.customers.create("عميل مؤقت")
        ctx.customers.delete(temp)
        assert ctx.customers.get(temp) is None

        # --- items: stock (with secondary unit) + service ------------------ #
        item = ctx.items.create(
            name="رز", category_id=category, item_type="مخزون",
            purchase_price=10.0, selling_price=15.0, quantity=24.0,
            base_unit_id=piece, barcode="FULL-1",
            item_units=[{"unit_id": carton, "conversion_factor": 12, "purchase_price": 100.0, "selling_price": 170.0}],
        )
        service = ctx.items.create(name="توصيل", item_type="خدمة", selling_price=5.0)

        # --- opening quantities -> inventory-only purchase invoice --------- #
        opening = ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
        assert opening is not None
        assert ctx.items.get(item)["opening_quantity"] == pytest.approx(0.0)
        assert ctx.items.get(item)["quantity"] == pytest.approx(24.0)

        # --- credit purchase in CARTONS (2 x 12 = 24 base units, 2x100=200) - #
        ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier,
            lines=[_line(item, 2, 100.0, unit_id=carton, desc="رز")],
        )
        item_row = ctx.items.get(item)
        assert item_row["quantity"] == pytest.approx(48.0)
        avg = float(item_row["average_cost"])
        assert avg == pytest.approx((24 * 10.0 + 200.0) / 48.0, abs=1e-6)

        # --- mixed sale (stock + service) with a partial down-payment ------ #
        sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[_line(item, 5, 15.0, unit_id=piece, desc="رز"), _line(service, 1, 5.0, desc="توصيل")],
        )

        # --- POS cash sale (no party, fully paid) -------------------------- #
        pos_sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=None, paid_amount=45.0,
            lines=[_line(item, 3, 15.0, unit_id=piece, desc="رز")],
        )

        # --- vouchers: customer receipt + supplier payment ----------------- #
        ctx.payments.create_voucher(
            voucher_type="receipt", amount=30.0, customer_id=customer,
            allocation_mode="manual", allocations={sale: 30.0},
        )
        ctx.payments.create_voucher(voucher_type="payment", amount=50.0, supplier_id=supplier)

        # --- expense ------------------------------------------------------- #
        ctx.expenses.create_expense(amount=20.0, description="كهرباء")

        # ---------------- invariants --------------------------------------- #
        # stock: 24 opening + 24 purchase - 5 - 3 sold = 40
        assert ctx.items.get(item)["quantity"] == pytest.approx(40.0)

        debit, credit = _trial(ctx)
        assert debit == pytest.approx(credit, abs=1e-6)

        # income statement
        income = ctx.reports.income_statement()
        assert income["sales"] == pytest.approx(125.0, abs=1e-6)  # 80 + 45
        assert income["purchases"] == pytest.approx(440.0, abs=1e-6)  # 240 opening + 200
        assert income["cogs"] == pytest.approx(8 * avg, abs=1e-6)
        assert income["gross_profit"] == pytest.approx(125.0 - 8 * avg, abs=1e-6)
        assert income["expenses"] == pytest.approx(20.0, abs=1e-6)
        assert income["net_profit"] == pytest.approx(125.0 - 8 * avg - 20.0, abs=1e-6)

        # inventory valuation == dashboard value == qty * avg
        valuation = ctx.reports.inventory_valuation()
        assert valuation["total_value"] == pytest.approx(40.0 * avg, abs=1e-6)
        assert ctx.dashboard.summary()["inventory_value"] == pytest.approx(40.0 * avg, abs=1e-6)

        # AR: sale 80 - receipt 30 = 50; AP: 200 - 50 = 150
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(50.0, abs=1e-6)
        assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(150.0, abs=1e-6)

        # cash: POS 45 + receipt 30 - payment 50 - expense 20 = 5
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(5.0, abs=1e-6)
        assert ctx.dashboard.summary()["cash"] == pytest.approx(5.0, abs=1e-6)

        # profitability reporting is consistent with the income statement
        inv_profit = {row["id"]: row for row in ctx.reports.invoice_profitability()}
        assert inv_profit[sale]["gross_profit"] == pytest.approx(80.0 - 5 * avg, abs=1e-6)
        assert inv_profit[pos_sale]["gross_profit"] == pytest.approx(45.0 - 3 * avg, abs=1e-6)
        item_profit = {row["item_name"]: row for row in ctx.reports.item_profitability()}
        assert item_profit["رز"]["gross_profit"] == pytest.approx(120.0 - 8 * avg, abs=1e-6)

        # --- currency: display never changes stored money ------------------ #
        stored = ctx.invoices.get_invoice(sale)["total"]
        assert stored == pytest.approx(80.0, abs=1e-6)
        currency.set_display_currency(ctx.settings, "USD")
        assert ctx.invoices.get_invoice(sale)["total"] == pytest.approx(stored, abs=1e-9)
        assert currency.format_amount(stored, ctx.settings) == "80.00 $"
        currency.set_display_currency(ctx.settings, "SYP")
        assert currency.format_amount(stored, ctx.settings) == "80,000 ل.س"

        # a SYP-era invoice keeps printing SYP after the switch (snapshot)
        currency.set_display_currency(ctx.settings, "USD")
        html = ctx.documents.invoice_html(sale)
        assert "ل.س" in html and "$" not in html.split("<body", 1)[-1][:2000]
        currency.set_display_currency(ctx.settings, "SYP")

        # --- rebuild is idempotent ----------------------------------------- #
        before = (ctx.dashboard.summary()["inventory_value"], ctx.reports.cash_movement()["closing_balance"])
        ctx.invoices.rebuild_derived_state()
        ctx.expenses.rebuild_ledger()
        assert ctx.dashboard.summary()["inventory_value"] == pytest.approx(before[0], abs=1e-6)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(before[1], abs=1e-6)
        d2, c2 = _trial(ctx)
        assert d2 == pytest.approx(c2, abs=1e-6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)