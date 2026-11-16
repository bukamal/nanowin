"""Deep, broad workflow matrix for the whole suite.

One place that drives every requested surface end-to-end against a real
AppContext/SQLite and asserts the shared accounting invariants after each
scenario:

* new sale + purchase invoices, cash vs credit, supplier/customer parties
* opening quantities ("أول المدة" / كميات افتتاحية) and their conversion
* sub-units (carton/sack conversion) on both purchase and sale
* composite/assembly items (recipes, sub-recipes, frozen history, blocked
  purchase, oversell guard)
* POS catalog / barcode lookup and the cash drawer + day close
* party statements, vouchers, manual + oldest allocation and the
  over-allocation guard
* restaurant mode (open orders, kitchen station tickets, tables, a composite
  menu item, partial settlement, order cancellation, audit chain)
* the table invoice sheet with several in-sheet tabs (restore + prune)

Fixture-free (builds/removes its own temp dirs) like the other integration
tests, so it runs under pytest and the standalone runner alike.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import flet as ft
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import invariants  # noqa: E402

from nano_offline.app_context import AppContext  # noqa: E402
from nano_offline.core import currency, restaurant_settings  # noqa: E402
from nano_offline.core.audit_chain import verify_audit_chain  # noqa: E402
from nano_offline.services.invoice_service import InvoiceLineInput  # noqa: E402

PASSWORD = "StrongPass1"


def _ctx(prefix: str = "nano-deep-") -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix=prefix)
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.auth.login("admin", PASSWORD)
    return ctx, tmp


def _line(item_id, qty, price, *, unit=None, desc="بند") -> InvoiceLineInput:
    return InvoiceLineInput(description=desc, item_id=item_id, quantity=qty, unit_price=price, unit_id=unit)


def _qty(ctx: AppContext, item_id: int) -> float:
    return float(ctx.items.get(item_id)["quantity"])


def _avg(ctx: AppContext, item_id: int) -> float:
    return float(ctx.items.get(item_id)["average_cost"])


# --------------------------------------------------------------------------- #
# 1. The integrated core cycle: sale/purchase, opening, credit/cash, parties.  #
# --------------------------------------------------------------------------- #
def test_integrated_cycle_sale_purchase_opening_credit_cash_parties():
    ctx, tmp = _ctx()
    try:
        piece = ctx.definitions.create_unit("قطعة", "ق")
        carton = ctx.definitions.create_unit("كرتونة", "ك")
        category = ctx.definitions.create_category("مواد غذائية")
        customer = ctx.customers.create("مقهى النور", "0900")
        supplier = ctx.suppliers.create("مورد الجملة", "0911")
        assert {u["name"] for u in ctx.definitions.list_units()} >= {"قطعة", "كرتونة"}

        rice = ctx.items.create(
            name="رز", category_id=category, item_type="مخزون",
            purchase_price=10.0, selling_price=15.0, quantity=24.0,
            base_unit_id=piece, barcode="RICE-1",
            item_units=[{"unit_id": carton, "conversion_factor": 12, "purchase_price": 100.0, "selling_price": 170.0}],
        )
        service = ctx.items.create(name="توصيل", item_type="خدمة", selling_price=5.0)

        # --- opening quantities -> inventory-only "أول المدة" purchase ------ #
        opening = ctx.invoices.convert_opening_balances(item_ids=[rice], reference="أول المدة")
        opening_row = ctx.invoices.get_invoice(opening)
        assert opening_row["type"] == "purchase"
        assert opening_row["supplier_id"] is None
        assert "__opening_balance__" in (opening_row["notes"] or "")
        assert float(opening_row["total"]) == pytest.approx(240.0)
        assert float(opening_row["paid_amount"]) == pytest.approx(240.0)
        assert ctx.items.get(rice)["opening_quantity"] == pytest.approx(0.0)
        assert ctx.items.get(rice)["quantity"] == pytest.approx(24.0)
        # opening is inventory-only: no payable, no cash, no period purchase
        assert ctx.suppliers.get(supplier)["balance"] == pytest.approx(0.0)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(0.0)
        assert ctx.dashboard.summary()["cash"] == pytest.approx(0.0)
        assert ctx.reports.income_statement()["purchases"] == pytest.approx(0.0)
        assert ctx.reports.income_statement()["opening_inventory"] == pytest.approx(240.0)

        # --- credit purchase in CARTONS (sub-unit) -> 2*12=24 base, 200 --- #
        ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier,
            lines=[_line(rice, 2, 100.0, unit=carton, desc="رز (كرتونة)")],
        )
        avg1 = (24 * 10.0 + 200.0) / 48.0
        assert _qty(ctx, rice) == pytest.approx(48.0)
        assert _avg(ctx, rice) == pytest.approx(avg1)
        assert ctx.suppliers.get(supplier)["balance"] == pytest.approx(200.0)
        assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(200.0)

        # --- credit sale (receivable) -------------------------------------- #
        sale1 = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer,
            lines=[_line(rice, 5, 15.0, unit=piece)],
        )
        assert ctx.customers.get(customer)["balance"] == pytest.approx(75.0)

        # --- mixed sale (stock + service) with a partial down-payment ------ #
        sale2 = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=10.0,
            lines=[_line(rice, 2, 15.0, unit=piece), _line(service, 1, 5.0, desc="توصيل")],
        )
        assert float(ctx.invoices.get_invoice(sale2)["total"]) == pytest.approx(35.0)
        assert float(ctx.invoices.get_invoice(sale2)["paid_amount"]) == pytest.approx(10.0)
        assert ctx.customers.get(customer)["balance"] == pytest.approx(100.0)

        # --- anonymous cash sale (no party -> always fully settled) -------- #
        pos = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=None, paid_amount=45.0,
            lines=[_line(rice, 3, 15.0, unit=piece)],
        )
        assert ctx.invoices.get_invoice(pos)["customer_id"] is None
        assert float(ctx.invoices.get_invoice(pos)["paid_amount"]) == pytest.approx(45.0)

        # --- anonymous cash purchase (no party -> fully settled) ----------- #
        anon_purchase = ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=None,
            lines=[_line(rice, 4, 10.0, unit=piece)],
        )
        assert ctx.invoices.get_invoice(anon_purchase)["supplier_id"] is None
        assert float(ctx.invoices.get_invoice(anon_purchase)["paid_amount"]) == pytest.approx(40.0)

        # --- vouchers + expense -------------------------------------------- #
        ctx.payments.create_voucher(
            voucher_type="receipt", amount=30.0, customer_id=customer,
            allocation_mode="manual", allocations={sale1: 30.0},
        )
        ctx.payments.create_voucher(voucher_type="payment", amount=50.0, supplier_id=supplier)
        ctx.expenses.create_expense(amount=20.0, description="كهرباء")

        # ---------------- stock / cost ------------------------------------- #
        final_avg = (38 * avg1 + 40.0) / 42.0
        assert _qty(ctx, rice) == pytest.approx(42.0)
        assert _avg(ctx, rice) == pytest.approx(final_avg)
        assert ctx.reports.inventory_valuation()["total_value"] == pytest.approx(42.0 * final_avg)
        assert ctx.dashboard.summary()["inventory_value"] == pytest.approx(42.0 * final_avg)

        # ---------------- ledger / statements ------------------------------ #
        cogs = 10.0 * avg1  # 5 + 2 + 3 pieces sold at avg1
        income = ctx.reports.income_statement()
        assert income["sales"] == pytest.approx(155.0)
        assert income["purchases"] == pytest.approx(240.0)      # 200 credit + 40 cash
        assert income["opening_inventory"] == pytest.approx(240.0)
        assert income["cogs"] == pytest.approx(cogs)
        assert income["gross_profit"] == pytest.approx(155.0 - cogs)
        assert income["expenses"] == pytest.approx(20.0)
        assert income["net_profit"] == pytest.approx(155.0 - cogs - 20.0)

        # AR: 75 + 35 - 30 (receipt) - 10 (initial) = 70
        assert ctx.customers.get(customer)["balance"] == pytest.approx(70.0)
        assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(70.0)
        # AP: 200 - 50 = 150
        assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(150.0)

        # cash: +10 (initial) +45 (POS) -40 (cash purchase) +30 (receipt) -50 (payment) -20 (expense) = -25
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(-25.0)
        assert ctx.dashboard.summary()["cash"] == pytest.approx(-25.0)

        # per-invoice / per-item profitability
        inv_profit = {row["id"]: row for row in ctx.reports.invoice_profitability()}
        assert inv_profit[sale1]["gross_profit"] == pytest.approx(75.0 - 5 * avg1)
        assert inv_profit[pos]["gross_profit"] == pytest.approx(45.0 - 3 * avg1)
        item_profit = {row["item_name"]: row for row in ctx.reports.item_profitability()}
        assert item_profit["رز"]["gross_profit"] == pytest.approx(150.0 - 10 * avg1)

        # customer statement closing matches the balance
        statement = ctx.statements.party_statement("customer", customer)
        assert statement["closing_balance"] == pytest.approx(70.0)
        assert statement["current_balance"] == pytest.approx(70.0)
        assert {int(r["id"]) for r in statement["open_invoices"]} == {sale1, sale2}

        invariants.assert_all(ctx)
        invariants.assert_rebuild_idempotent(ctx)

        # --- edit + delete keep the books honest --------------------------- #
        ctx.invoices.update_invoice(
            sale1, invoice_type="sale", customer_id=customer,
            lines=[_line(rice, 3, 15.0, unit=piece)],
        )
        assert float(ctx.invoices.get_invoice(sale1)["total"]) == pytest.approx(45.0)
        assert _qty(ctx, rice) == pytest.approx(44.0)
        invariants.assert_all(ctx)

        ctx.invoices.delete_invoice(pos)
        assert ctx.invoices.get_invoice(pos) is None
        invariants.assert_all(ctx)
        invariants.assert_rebuild_idempotent(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 2. Opening quantities: multiple items, exclusions, idempotency.             #
# --------------------------------------------------------------------------- #
def test_opening_quantities_cover_stock_only_and_convert_once():
    ctx, tmp = _ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        first = ctx.items.create(name="مادة أ", item_type="مخزون", purchase_price=4.0, quantity=10.0, base_unit_id=unit)
        second = ctx.items.create(name="مادة ب", item_type="مخزون", purchase_price=6.0, quantity=5.0, base_unit_id=unit)
        service = ctx.items.create(name="خدمة", item_type="خدمة", selling_price=3.0)
        leaf = ctx.items.create(name="ورقة", item_type="مخزون", purchase_price=1.0, quantity=8.0, base_unit_id=unit)
        composite = ctx.items.create(
            name="طقم", item_type="مخزون", selling_price=20.0, is_composite=True,
            components=[{"component_item_id": leaf, "quantity": 2, "conversion_factor": 1}],
        )

        invoice_id = ctx.invoices.convert_opening_balances(
            item_ids=[first, second, service, composite], reference="أول المدة"
        )
        assert invoice_id is not None
        row = ctx.invoices.get_invoice(invoice_id)
        assert float(row["total"]) == pytest.approx(10 * 4.0 + 5 * 6.0)  # service + composite excluded
        assert {int(ln["item_id"]) for ln in row["lines"]} == {first, second}

        # idempotent: running again changes nothing
        assert ctx.invoices.convert_opening_balances(item_ids=[first, second]) is None
        assert float(ctx.invoices.get_invoice(invoice_id)["total"]) == pytest.approx(70.0)

        # the composite still holds no stock of its own
        assert _qty(ctx, composite) == pytest.approx(0.0)
        assert _qty(ctx, leaf) == pytest.approx(8.0)
        invariants.assert_all(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3. Composite / assembly recipes, sub-recipes, frozen history, guards.       #
# --------------------------------------------------------------------------- #
def test_composite_recipes_subunits_frozen_history_and_guards():
    ctx, tmp = _ctx()
    try:
        kg = ctx.definitions.create_unit("كيلو", "كغ")
        sack = ctx.definitions.create_unit("كيس", "كيس")
        customer = ctx.customers.create("عميل جملة")

        flour = ctx.items.create(
            name="طحين", item_type="مخزون", purchase_price=2.0, selling_price=3.0, quantity=100.0,
            base_unit_id=kg, item_units=[{"unit_id": sack, "conversion_factor": 50, "purchase_price": 90.0}],
        )
        sugar = ctx.items.create(name="سكر", item_type="مخزون", purchase_price=5.0, selling_price=8.0, quantity=100.0, base_unit_id=kg)

        # 1 sack (x50) flour + 2 kg sugar == 110 cost per cake
        cake = ctx.items.create(
            name="كعكة", item_type="مخزون", selling_price=150.0, is_composite=True,
            components=[
                {"component_item_id": flour, "quantity": 1, "unit_id": sack, "conversion_factor": 50},
                {"component_item_id": sugar, "quantity": 2, "unit_id": kg, "conversion_factor": 1},
            ],
        )

        invoice_id = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer,
            lines=[_line(cake, 2, 150.0, desc="كعكة")],
        )
        assert _qty(ctx, flour) == pytest.approx(0.0)     # 2 * 50
        assert _qty(ctx, sugar) == pytest.approx(96.0)    # 2 * 2
        assert _qty(ctx, cake) == pytest.approx(0.0)      # composites hold no stock
        line = ctx.invoices.get_invoice(invoice_id)["lines"][0]
        assert line["cost_amount"] == pytest.approx(2 * (50 * 2.0) + 4 * 5.0)  # 220
        assert line["unit_cost"] == pytest.approx(110.0)

        # oversell blocked, state untouched
        with pytest.raises(ValueError):
            ctx.invoices.create_invoice(
                invoice_type="sale", customer_id=customer, lines=[_line(cake, 1, 150.0, desc="كعكة")]
            )
        assert _qty(ctx, flour) == pytest.approx(0.0)
        assert _qty(ctx, sugar) == pytest.approx(96.0)

        # edit the recipe: already-issued invoices keep their frozen components
        ctx.items.update(
            cake, name="كعكة", item_type="مخزون", selling_price=150.0, is_composite=True,
            components=[{"component_item_id": sugar, "quantity": 5, "conversion_factor": 1}],
        )
        ctx.invoices.rebuild_derived_state()
        assert _qty(ctx, flour) == pytest.approx(0.0)
        assert _qty(ctx, sugar) == pytest.approx(96.0)

        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, lines=[_line(cake, 1, 150.0, desc="كعكة")]
        )
        assert _qty(ctx, sugar) == pytest.approx(91.0)  # new recipe consumes 5
        assert _qty(ctx, flour) == pytest.approx(0.0)

        # composites can never be purchased
        with pytest.raises(ValueError):
            ctx.invoices.create_invoice(
                invoice_type="purchase", supplier_id=None, lines=[_line(cake, 1, 100.0)]
            )

        # multi-level sub-recipe explodes to the leaf material
        leaf = ctx.items.create(name="خام", item_type="مخزون", purchase_price=1.0, quantity=100.0, base_unit_id=kg)
        sub = ctx.items.create(
            name="نصف مصنّع", item_type="مخزون", selling_price=10.0, is_composite=True,
            components=[{"component_item_id": leaf, "quantity": 2, "conversion_factor": 1}],
        )
        top = ctx.items.create(
            name="منتج نهائي", item_type="مخزون", selling_price=30.0, is_composite=True,
            components=[{"component_item_id": sub, "quantity": 3, "conversion_factor": 1}],
        )
        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=None, paid_amount=30.0, lines=[_line(top, 1, 30.0)]
        )
        assert _qty(ctx, leaf) == pytest.approx(94.0)  # 1 * 3 * 2

        assert verify_audit_chain(ctx.db)["valid"] is True
        invariants.assert_all(ctx)
        invariants.assert_rebuild_idempotent(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 4. POS catalog, barcode lookup, cash drawer and day close.                  #
# --------------------------------------------------------------------------- #
def test_pos_catalog_barcode_cash_drawer_and_day_close():
    ctx, tmp = _ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        a = ctx.items.create(name="أرز", item_type="مخزون", purchase_price=2.0, selling_price=5.0, quantity=100.0, base_unit_id=unit, barcode="POS-A")
        b = ctx.items.create(name="برغل", item_type="مخزون", purchase_price=3.0, selling_price=6.0, quantity=100.0, base_unit_id=unit, barcode="POS-B")
        c = ctx.items.create(name="تمر", item_type="مخزون", purchase_price=1.0, selling_price=4.0, quantity=100.0, base_unit_id=unit, barcode="POS-C")

        # three anonymous cash sales with different volumes
        total = 0.0
        for item, qty in ((a, 5), (b, 3), (c, 1)):
            price = float(ctx.items.get(item)["selling_price"])
            ctx.invoices.create_invoice(
                invoice_type="sale", customer_id=None, paid_amount=qty * price,
                lines=[_line(item, qty, price)],
            )
            total += qty * price

        order = [row["name"] for row in ctx.items.pos_catalog()]
        assert order[:3] == ["أرز", "برغل", "تمر"]  # best sellers first

        found = ctx.items.find_by_barcode("POS-C")
        assert found is not None and found["id"] == c
        assert ctx.items.find_by_barcode("NOPE") is None

        assert ctx.cash_day_close.book_cash() == pytest.approx(total)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(total)
        movement = ctx.cash_day_close.today_cash_movement()
        assert movement["inflow"] == pytest.approx(total)
        assert movement["net"] == pytest.approx(total)

        # exact count -> no adjustment, invariants intact
        result = ctx.cash_day_close.close_day(counted_cash=total, note="إغلاق")
        assert result["variance"] == pytest.approx(0.0)
        assert result["adjustment_posted"] is False
        assert ctx.cash_day_close.last_close()["counted_cash"] == pytest.approx(total)
        invariants.assert_all(ctx)

        # recorded variance without posting keeps the books balanced too
        result = ctx.cash_day_close.close_day(counted_cash=total + 7.0, post_adjustment=False)
        assert result["variance"] == pytest.approx(7.0)
        assert ctx.cash_day_close.book_cash() == pytest.approx(total)
        invariants.assert_all(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_day_close_cash_adjustment_keeps_trial_balance():
    ctx, tmp = _ctx(prefix="nano-dayclose-")
    try:
        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=None, paid_amount=30.0,
            lines=[InvoiceLineInput(description="x", quantity=2, unit_price=15.0)],
        )
        book = ctx.cash_day_close.book_cash()
        assert book == pytest.approx(30.0)

        # surplus: counted above book -> CASH_OVERAGE (other income)
        over = ctx.cash_day_close.close_day(counted_cash=book + 5.0, post_adjustment=True)
        assert over["variance"] == pytest.approx(5.0)
        assert over["adjustment_posted"] is True
        assert ctx.cash_day_close.book_cash() == pytest.approx(book + 5.0)
        assert ctx.statements.trial_balance()["balanced"] is True
        invariants.assert_trial_balance(ctx)
        invariants.assert_cash_matches_dashboard(ctx)

        # shortfall: counted below book -> CASH_SHORTAGE (other expense)
        book2 = ctx.cash_day_close.book_cash()
        short = ctx.cash_day_close.close_day(counted_cash=book2 - 3.0, post_adjustment=True)
        assert short["variance"] == pytest.approx(-3.0)
        assert ctx.cash_day_close.book_cash() == pytest.approx(book2 - 3.0)
        assert ctx.statements.trial_balance()["balanced"] is True
        invariants.assert_trial_balance(ctx)

        # both variance accounts are labelled in the trial balance
        codes = {row["code"] for row in ctx.statements.trial_balance()["accounts"]}
        assert {"CASH_OVERAGE", "CASH_SHORTAGE"} <= codes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 5. Party statements, vouchers, allocation modes, over-allocation guard.      #
# --------------------------------------------------------------------------- #
def test_party_statements_vouchers_and_allocation_guards():
    ctx, tmp = _ctx()
    try:
        customer = ctx.customers.create("عميل الائتمان")
        supplier = ctx.suppliers.create("مورد الائتمان")

        inv1 = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, invoice_date="2026-01-01",
            lines=[InvoiceLineInput(description="x", quantity=4, unit_price=25.0)],  # 100
        )
        inv2 = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, invoice_date="2026-02-01",
            lines=[InvoiceLineInput(description="y", quantity=3, unit_price=20.0)],  # 60
        )
        assert ctx.customers.get(customer)["balance"] == pytest.approx(160.0)

        # manual receipt split across both invoices
        ctx.payments.create_voucher(
            voucher_type="receipt", amount=80.0, customer_id=customer,
            allocation_mode="manual", allocations={inv1: 50.0, inv2: 30.0},
        )
        assert ctx.customers.get(customer)["balance"] == pytest.approx(80.0)

        statement = ctx.statements.party_statement("customer", customer)
        assert statement["closing_balance"] == pytest.approx(80.0)
        open_remaining = {int(r["id"]): float(r["remaining_amount"]) for r in statement["open_invoices"]}
        assert open_remaining == {inv1: pytest.approx(50.0), inv2: pytest.approx(30.0)}

        aging = ctx.reports.aging_summary("customer")
        assert aging["total"] == pytest.approx(80.0)
        assert round(sum(aging["buckets"].values()), 6) == pytest.approx(80.0)

        # manual allocation beyond a single invoice's remaining is refused
        with pytest.raises(ValueError):
            ctx.payments.create_voucher(
                voucher_type="receipt", amount=60.0, customer_id=customer,
                allocation_mode="manual", allocations={inv1: 60.0},  # inv1 only 50 left
            )

        # supplier payment auto-allocates oldest-first
        purchase = ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier,
            lines=[InvoiceLineInput(description="z", quantity=20, unit_price=10.0)],  # 200
        )
        ctx.payments.create_voucher(voucher_type="payment", amount=120.0, supplier_id=supplier)
        assert ctx.suppliers.get(supplier)["balance"] == pytest.approx(80.0)
        assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(80.0)
        assert float(ctx.invoices.get_invoice(purchase)["paid_amount"]) == pytest.approx(120.0)

        # a receipt must belong to a customer, a payment to a supplier
        with pytest.raises(ValueError):
            ctx.payments.create_voucher(voucher_type="receipt", amount=10.0, supplier_id=supplier)
        with pytest.raises(ValueError):
            ctx.payments.create_voucher(voucher_type="payment", amount=10.0, customer_id=customer)
        with pytest.raises(ValueError):
            ctx.payments.create_voucher(voucher_type="receipt", amount=0.0, customer_id=customer)

        invariants.assert_all(ctx)
        invariants.assert_rebuild_idempotent(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 6. Currency: display switching never rewrites stored money; snapshot prints. #
# --------------------------------------------------------------------------- #
def test_currency_display_and_invoice_snapshot():
    ctx, tmp = _ctx(prefix="nano-cur-")
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        item = ctx.items.create(name="مادة", item_type="مخزون", purchase_price=1.0, selling_price=2.0, quantity=100.0, base_unit_id=unit)

        currency.set_display_currency(ctx.settings, "SYP")
        currency.set_exchange_rate(ctx.settings, 1000)
        sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=None, paid_amount=10.0,
            lines=[_line(item, 5, 2.0)],
        )
        assert float(ctx.invoices.get_invoice(sale)["total"]) == pytest.approx(10.0)
        assert currency.format_amount(10.0, ctx.settings) == "10,000 ل.س"

        currency.set_display_currency(ctx.settings, "USD")
        assert float(ctx.invoices.get_invoice(sale)["total"]) == pytest.approx(10.0)
        assert currency.format_amount(10.0, ctx.settings) == "10.00 $"
        # the SYP-era invoice still prints ل.س after the display switch (snapshot)
        assert "ل.س" in ctx.documents.invoice_html(sale)
        invariants.assert_all(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 7. Validation matrix: every malformed input is refused without side effects.#
# --------------------------------------------------------------------------- #
def test_validation_matrix_rejects_bad_input_without_side_effects():
    ctx, tmp = _ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        other_unit = ctx.definitions.create_unit("علبة", "علبة")
        customer = ctx.customers.create("عميل")
        supplier = ctx.suppliers.create("مورد")
        item = ctx.items.create(name="مادة", item_type="مخزون", purchase_price=10.0, selling_price=15.0, quantity=20.0, base_unit_id=unit, barcode="VAL-1")

        def _ok():
            return ctx.items.get(item)["quantity"]

        baseline = _ok()

        with pytest.raises(ValueError):  # empty lines
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=customer, lines=[])
        with pytest.raises(ValueError):  # bad type
            ctx.invoices.create_invoice(invoice_type="return", lines=[_line(item, 1, 15.0)])
        with pytest.raises(ValueError):  # sale bound to a supplier
            ctx.invoices.create_invoice(invoice_type="sale", supplier_id=supplier, paid_amount=15.0, lines=[_line(item, 1, 15.0)])
        with pytest.raises(ValueError):  # purchase bound to a customer
            ctx.invoices.create_invoice(invoice_type="purchase", customer_id=customer, paid_amount=15.0, lines=[_line(item, 1, 15.0)])
        with pytest.raises(ValueError):  # zero quantity
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=customer, lines=[_line(item, 0, 15.0)])
        with pytest.raises(ValueError):  # negative price
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=customer, lines=[_line(item, 1, -1.0)])
        with pytest.raises(ValueError):  # over-payment
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=customer, paid_amount=100.0, lines=[_line(item, 1, 15.0)])
        with pytest.raises(ValueError):  # unknown unit for the item
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=customer, paid_amount=15.0, lines=[_line(item, 1, 15.0, unit=other_unit)])
        with pytest.raises(ValueError):  # unknown item
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=customer, paid_amount=15.0, lines=[_line(999_999, 1, 15.0)])
        with pytest.raises(ValueError):  # unknown party
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=999_999, lines=[_line(item, 1, 15.0)])
        with pytest.raises(ValueError):  # oversell without permission
            ctx.invoices.create_invoice(invoice_type="sale", customer_id=customer, lines=[_line(item, 999, 15.0)])

        # no malformed attempt moved stock
        assert _ok() == pytest.approx(baseline)

        # negative stock is allowed only when explicitly permitted
        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, allow_negative_stock=True,
            lines=[_line(item, 25, 15.0)],
        )
        assert _qty(ctx, item) == pytest.approx(-5.0)
        invariants.assert_all(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 7. Restaurant mode: open orders, station tickets, composites, settlement.   #
# --------------------------------------------------------------------------- #
def test_restaurant_mode_open_order_composite_menu_settlement_and_cancel():
    ctx, tmp = _ctx(prefix="nano-resto-")
    try:
        ctx.section_print.auto_dispatch = False
        restaurant_settings.set_enabled(ctx.settings, True)
        restaurant_settings.set_use_tables(ctx.settings, True)
        restaurant_settings.set_flag(ctx.settings, restaurant_settings.ALLOW_PARTIAL_KEY, True)
        assert restaurant_settings.enabled(ctx.settings) and restaurant_settings.use_tables(ctx.settings)

        kitchen = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
        bar = ctx.sections.create(name="المشروبات", printer_host="10.0.0.2")
        assert ctx.tables.create_many(area="صالة", start=1, count=4) == 4

        burger = ctx.items.create(name="برغر", item_type="مخزون", quantity=100.0, purchase_price=4.0, selling_price=10.0, section_id=kitchen)
        cola = ctx.items.create(name="كولا", item_type="مخزون", quantity=100.0, purchase_price=1.0, selling_price=3.0, section_id=bar)
        meal = ctx.items.create(
            name="وجبة", item_type="مخزون", selling_price=20.0, is_composite=True, section_id=kitchen,
            components=[
                {"component_item_id": burger, "quantity": 2, "conversion_factor": 1},
                {"component_item_id": cola, "quantity": 1, "conversion_factor": 1},
            ],
        )

        # open an unpaid order on table "1"
        order = ctx.invoices.create_invoice(
            invoice_type="sale", lines=[_line(meal, 1, 20.0), _line(cola, 1, 3.0)],
            paid_amount=0, reference="1",
        )
        row = ctx.invoices.get_invoice(order)
        assert row["customer_id"] is not None                     # auto restaurant party
        assert float(row["paid_amount"]) == 0
        assert _qty(ctx, burger) == pytest.approx(98)             # composite consumption
        assert _qty(ctx, cola) == pytest.approx(98)               # 1 in meal + 1 direct
        assert ctx.sections.counts()["pending"] == 2              # kitchen + bar tickets

        # route the composite to the kitchen and drain the queue
        by_section = ctx.sections.invoice_section_lines(order)
        assert kitchen in by_section and bar in by_section
        ctx.section_print.dispatch_pending(sender=lambda *a: None)
        assert ctx.sections.counts()["pending"] == 0

        # settle the order in full through the party payment flow
        total = float(ctx.invoices.get_invoice(order)["total"])
        assert total == pytest.approx(23.0)
        ctx.payments.register_invoice_payment(order, total)
        settled = ctx.invoices.get_invoice(order)
        assert float(settled["paid_amount"]) == pytest.approx(total)
        assert ctx.invoices.open_sale_orders() == []

        # a second, still-unpaid order can be cancelled (stock comes back)
        order2 = ctx.invoices.create_invoice(
            invoice_type="sale", lines=[_line(burger, 1, 10.0)], paid_amount=0, reference="2"
        )
        ctx.section_print.dispatch_pending(sender=lambda *a: None)
        assert _qty(ctx, burger) == pytest.approx(97)
        ctx.invoices.cancel_sale_order(order2, reason="خطأ في الطلب", print_void=False)
        assert ctx.invoices.get_invoice(order2) is None
        assert _qty(ctx, burger) == pytest.approx(98)

        assert verify_audit_chain(ctx.db)["valid"] is True
        invariants.assert_all(ctx)
        invariants.assert_rebuild_idempotent(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 8. The table invoice sheet: several in-sheet tabs restore + stale prune.    #
# --------------------------------------------------------------------------- #
class FakePage:
    """Minimal stand-in for ``ft.Page`` -- only what the sheet touches."""

    def __init__(self) -> None:
        self.overlay: list = []
        self.width = 1280
        self.height = 800
        self.on_keyboard_event = None
        self.updated = 0
        self.opened: list = []

    def update(self, *args, **kwargs):
        self.updated += 1

    def open(self, control):
        self.opened.append(control)
        try:
            control.open = True
        except Exception:
            pass

    @staticmethod
    def close(control):
        try:
            control.open = False
        except Exception:
            pass

    def run_task(self, handler, *args, **kwargs):
        try:
            result = handler(*args, **kwargs)
        except Exception:
            return
        close = getattr(result, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def run_thread(self, handler, *args, **kwargs):
        try:
            handler(*args, **kwargs)
        except Exception:
            pass

    def add(self, *controls, **kwargs):
        pass

    def scroll_to(self, **kwargs):
        pass

    def clean(self):
        pass


def _children(node):
    content = getattr(node, "content", None)
    if content is not None:
        if isinstance(content, (list, tuple)):
            yield from content
        else:
            yield content
    controls = getattr(node, "controls", None)
    if controls:
        yield from controls


def _find_tab_strip(root):
    candidates: list[tuple[int, ft.Row]] = []

    def walk(node):
        if isinstance(node, ft.Row) and getattr(node, "scroll", None) == ft.ScrollMode.AUTO:
            kids = list(getattr(node, "controls", []) or [])
            chips = [k for k in kids if isinstance(k, ft.Container) and isinstance(getattr(k, "content", None), ft.Row)]
            has_add = any(
                isinstance(k, ft.Container) and isinstance(getattr(k, "content", None), ft.Icon) for k in kids
            )
            if chips or has_add:
                candidates.append((len(chips), node))
        for child in _children(node):
            walk(child)

    walk(root)
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _tab_labels(strip) -> list[str]:
    labels: list[str] = []
    for child in strip.controls:
        if isinstance(child, ft.Container) and isinstance(getattr(child, "content", None), ft.Row):
            body = child.content.controls[0]
            text = getattr(body, "content", None)
            labels.append(text.value if isinstance(text, ft.Text) else "")
    return labels


def _draft(invoice_id=None, inv_type="sale", lines=None, party_id=None, reference="", notes=""):
    return {
        "invoice_id": invoice_id,
        "type": inv_type,
        "party_id": party_id,
        "date": date.today().isoformat(),
        "reference": reference,
        "notes": notes,
        "paid": "0",
        "lines": lines or [],
    }


def test_table_invoice_sheet_tabs_restore_types_and_prune_stale():
    from nano_offline.views.invoice_view import InvoiceCenter

    ctx, tmp = _ctx(prefix="nano-tabs-")
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = InvoiceCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)

        unit = ctx.definitions.create_unit("قطعة", "ق")
        item = ctx.items.create(name="مادة", item_type="مخزون", purchase_price=2.0, selling_price=3.0, quantity=10.0, base_unit_id=unit, barcode="TAB-1")
        sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=None, paid_amount=4.0,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=2, unit_price=2.0, unit_id=unit)],
        )

        # a) a sale draft + a purchase blank tab survive; a stale edit tab is pruned
        sale_line = {"item": item, "description": "x", "unit": unit, "qty": 2, "price": 2.0}
        ctx.settings.set("ui.invoice_tabs", json.dumps({
            "items": [
                _draft(inv_type="sale", lines=[sale_line]),
                _draft(invoice_id=10_000_000, inv_type="purchase"),   # deleted invoice -> prune
                _draft(inv_type="purchase"),                           # blank -> reused
            ],
            "active": 0,
        }, ensure_ascii=False))
        center.show_table_invoice_sheet("purchase")
        labels = _tab_labels(_find_tab_strip(page.opened[-1].content))
        assert len(labels) == 2
        assert any("بيع" in label for label in labels)
        assert any("شراء" in label for label in labels)
        assert all("تعديل #10000000" not in label for label in labels)

        # b) reopening an existing invoice keeps exactly its edit tab
        ctx.settings.set("ui.invoice_tabs", json.dumps({
            "items": [_draft(invoice_id=sale, inv_type="sale", lines=[sale_line])],
            "active": 0,
        }, ensure_ascii=False))
        center.show_table_invoice_sheet("sale", invoice_id=sale)
        labels = _tab_labels(_find_tab_strip(page.opened[-1].content))
        assert len(labels) == 1 and labels[0].startswith("تعديل")

        # c) a fresh sale and a fresh purchase sheet both build their tab strip
        ctx.settings.set("ui.invoice_tabs", "")
        for kind in ("sale", "purchase"):
            center.show_table_invoice_sheet(kind)
            strip = _find_tab_strip(page.opened[-1].content)
            assert strip is not None
            assert len(_tab_labels(strip)) == 1  # exactly one fresh blank tab
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
