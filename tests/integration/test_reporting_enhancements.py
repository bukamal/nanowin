from __future__ import annotations

from datetime import date, timedelta

import pytest

from nano_offline.app_context import AppContext
from nano_offline.services.invoice_service import InvoiceLineInput


def _context(tmp_path):
    ctx = AppContext.create(tmp_path / "reports.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    unit = ctx.definitions.create_unit("قطعة", "ق")
    customer = ctx.customers.create("عميل")
    supplier = ctx.suppliers.create("مورد")
    item = ctx.items.create(
        name="مادة",
        item_type="مخزون",
        purchase_price=10,
        selling_price=20,
        quantity=10,
        base_unit_id=unit,
        barcode="REPORT-1",
    )
    opening_id = ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
    assert opening_id is not None
    return ctx, item, customer, supplier


def test_income_statement_comparison_and_aging_use_separate_opening_inventory(tmp_path):
    ctx, item, customer, supplier = _context(tmp_path)
    today = date.today()
    current_from = (today + timedelta(days=14)).isoformat()
    current_to = (today + timedelta(days=20)).isoformat()
    previous_from = (today + timedelta(days=7)).isoformat()
    previous_to = (today + timedelta(days=13)).isoformat()

    ctx.invoices.create_invoice(
        invoice_type="purchase",
        supplier_id=supplier,
        invoice_date=current_from,
        lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=5, unit_price=10)],
    )
    ctx.invoices.create_invoice(
        invoice_type="sale",
        customer_id=customer,
        invoice_date=current_from,
        lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=5, unit_price=20)],
    )
    ctx.invoices.create_invoice(
        invoice_type="sale",
        customer_id=customer,
        invoice_date=previous_from,
        lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=2, unit_price=25)],
    )
    ctx.expenses.create_expense(amount=20, description="كهرباء", expense_date=current_from)
    old_sale = ctx.invoices.create_invoice(
        invoice_type="sale",
        customer_id=customer,
        invoice_date=today.isoformat(),
        lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=1, unit_price=100)],
    )
    ctx.payments.create_voucher(
        voucher_type="receipt",
        amount=20,
        customer_id=customer,
        allocation_mode="manual",
        allocations={old_sale: 20},
    )

    report = ctx.reports.income_statement(date_from=current_from, date_to=current_to)
    assert report["sales"] == pytest.approx(100)
    assert report["cogs"] == pytest.approx(50)
    assert report["gross_profit"] == pytest.approx(50)
    assert report["expenses"] == pytest.approx(20)
    assert report["net_profit"] == pytest.approx(30)
    assert report["gross_margin_percent"] == pytest.approx(50)
    assert report["net_margin_percent"] == pytest.approx(30)
    assert report["markup_percent"] == pytest.approx(100)
    assert report["purchases"] == pytest.approx(50)
    assert report["opening_inventory"] == pytest.approx(100)

    comparison = ctx.reports.period_comparison(date_from=current_from, date_to=current_to)
    assert comparison["previous_from"] == previous_from
    assert comparison["previous_to"] == previous_to
    assert comparison["metrics"]["sales"]["previous"] == pytest.approx(50)
    assert comparison["metrics"]["sales"]["current"] == pytest.approx(100)
    assert ctx.reports.profit_trend(date_from=current_from, date_to=current_to)

    aging_as_of = (today + timedelta(days=95)).isoformat()
    aging = ctx.reports.aging_summary("customer", as_of=aging_as_of)
    assert aging["basis"] == "invoice_date"
    assert aging["buckets"]["90+"] == pytest.approx(80)
    assert any(row["id"] == old_sale and row["remaining_amount"] == pytest.approx(80) for row in aging["invoices"])

    outlook = ctx.reports.cash_outlook(as_of=current_to)
    assert outlook["scenario_only"] is True
    assert [row["days"] for row in outlook["projections"]] == [7, 30, 90]


def test_forecast_backtest_reports_error_only_with_enough_months(tmp_path):
    ctx = AppContext.create(tmp_path / "forecast.db")
    unit = ctx.definitions.create_unit("مرة", "م")
    service = ctx.items.create(name="استشارة", item_type="خدمة", selling_price=10, purchase_price=0, base_unit_id=unit)
    customer = ctx.customers.create("عميل")
    today = date.today()
    for months_ago, amount in ((4, 100), (3, 120), (2, 80), (1, 150), (0, 130)):
        month_index = today.year * 12 + today.month - 1 - months_ago
        year, month = divmod(month_index, 12)
        invoice_date = date(year, month + 1, 1).isoformat()
        ctx.invoices.create_invoice(
            invoice_type="sale",
            customer_id=customer,
            invoice_date=invoice_date,
            lines=[InvoiceLineInput(description="استشارة", item_id=service, quantity=1, unit_price=amount)],
        )

    result = ctx.forecast.forecast_accuracy(invoice_type="sale")
    assert result["insufficient"] is False
    assert result["sample_months"] == 3
    assert result["mae"] > 0
    assert result["wape_percent"] > 0
    assert 0 <= result["accuracy_percent"] <= 100
