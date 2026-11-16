"""Party opening balances + per-party currency (schema 26).

Opening balances are posted as a balanced AR/AP vs OPENING_EQUITY pair -- never
as a fake sale/purchase -- so they never touch cash or the income statement and
survive every rebuild. A party's fixed currency pins its invoices/vouchers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import invariants  # noqa: E402

from nano_offline.app_context import AppContext  # noqa: E402
from nano_offline.core import currency  # noqa: E402
from nano_offline.services.invoice_service import InvoiceLineInput  # noqa: E402


def _ctx(tmp_path) -> AppContext:
    ctx = AppContext.create(tmp_path / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx


def _sale(ctx: AppContext, customer_id: int, total: float = 30.0, date: str | None = None) -> int:
    units = ctx.definitions.list_units()
    unit = int(units[0]["id"]) if units else ctx.definitions.create_unit("قطعة", "ق")
    existing = next((row for row in ctx.items.list() if row["name"] == "مادة"), None)
    item = int(existing["id"]) if existing else ctx.items.create(
        name="مادة", purchase_price=1.0, selling_price=1.0, quantity=100.0, base_unit_id=unit
    )
    return ctx.invoices.create_invoice(
        invoice_type="sale", customer_id=customer_id, paid_amount=0, invoice_date=date,
        lines=[InvoiceLineInput(description="x", item_id=item, quantity=total, unit_price=1.0, unit_id=unit)],
    )


def test_party_opening_balances_are_balanced_and_never_touch_cash_or_pnl(tmp_path):
    ctx = _ctx(tmp_path)
    debit_customer = ctx.customers.create("مدين", opening_balance=120.0, opening_balance_date="2026-01-01")
    credit_customer = ctx.customers.create("دائن", opening_balance=-30.0)
    credit_supplier = ctx.suppliers.create("مورد دائن", opening_balance=80.0)
    debit_supplier = ctx.suppliers.create("مورد مدين", opening_balance=-20.0)

    assert ctx.customers.get(debit_customer)["balance"] == pytest.approx(120.0)
    assert ctx.customers.get(credit_customer)["balance"] == pytest.approx(-30.0)
    assert ctx.suppliers.get(credit_supplier)["balance"] == pytest.approx(80.0)
    assert ctx.suppliers.get(debit_supplier)["balance"] == pytest.approx(-20.0)

    # AR 120 debit + 30 credit => 90 net; AP 80 credit + 20 debit => 60 net.
    assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(90.0)
    assert ctx.reports.party_balances("supplier")["net_total"] == pytest.approx(60.0)
    # Opening balances are not a period purchase/sale and move no cash.
    assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(0.0)
    assert ctx.reports.income_statement()["sales"] == pytest.approx(0.0)
    assert ctx.reports.income_statement()["purchases"] == pytest.approx(0.0)
    assert ctx.dashboard.summary()["cash"] == pytest.approx(0.0)

    invariants.assert_all(ctx)
    invariants.assert_rebuild_idempotent(ctx)
    invariants.assert_all(ctx)


def test_opening_balance_edit_after_activity_reconciles(tmp_path):
    ctx = _ctx(tmp_path)
    customer = ctx.customers.create("عميل", opening_balance=100.0, opening_balance_date="2026-01-01")
    _sale(ctx, customer, total=30.0)
    assert ctx.customers.get(customer)["balance"] == pytest.approx(130.0)

    ctx.customers.update(customer, "عميل", opening_balance=70.0, opening_balance_date="2026-01-01")
    assert ctx.customers.get(customer)["balance"] == pytest.approx(100.0)  # 70 + 30

    invariants.assert_all(ctx)
    invariants.assert_rebuild_idempotent(ctx)
    invariants.assert_all(ctx)


def test_party_currency_pins_invoices_and_vouchers(tmp_path):
    ctx = _ctx(tmp_path)
    currency.set_display_currency(ctx.settings, "USD")
    currency.set_exchange_rate(ctx.settings, 1000)

    syp_party = ctx.customers.create("زبون ليري", currency="SYP")
    usd_party = ctx.customers.create("زبون دولاري", currency="USD")
    inherit_party = ctx.customers.create("زبون موروث")

    syp_sale = _sale(ctx, syp_party, total=10.0)
    usd_sale = _sale(ctx, usd_party, total=10.0)
    inherit_sale = _sale(ctx, inherit_party, total=10.0)

    syp_row = ctx.invoices.get_invoice(syp_sale)
    assert syp_row["invoice_currency_code"] == "SYP"
    assert syp_row["invoice_currency_symbol"] == currency.DEFAULT_DISPLAY_SYMBOL
    assert float(syp_row["invoice_exchange_rate"]) == pytest.approx(1000.0)

    usd_row = ctx.invoices.get_invoice(usd_sale)
    assert usd_row["invoice_currency_code"] == "USD"
    assert inherit_sale and ctx.invoices.get_invoice(inherit_sale)["invoice_currency_code"] == "USD"

    voucher = ctx.payments.create_voucher(voucher_type="receipt", amount=5.0, customer_id=syp_party)
    with ctx.db.connect() as conn:
        payment = conn.execute(
            "SELECT payment_currency_code FROM payments WHERE source_type='voucher' AND source_id=?",
            (voucher,),
        ).fetchone()
    assert payment["payment_currency_code"] == "SYP"

    invariants.assert_all(ctx)


def test_party_field_validation(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError):
        ctx.customers.create("خاطئ", currency="EUR")
    with pytest.raises(ValueError):
        ctx.customers.create("خاطئ", credit_limit=-5)
    with pytest.raises(ValueError):
        ctx.suppliers.create("خاطئ", opening_balance=float("nan"))


def test_bulk_create_parties_with_openings(tmp_path):
    ctx = _ctx(tmp_path)
    rows = [
        ("عميل 1", 50.0, "debit"),
        ("عميل 2", 25.0, "credit"),
        ("عميل 3", 0.0, "debit"),
    ]
    for name, magnitude, side in rows:
        natural = magnitude if side == "debit" else -magnitude
        ctx.customers.create(name, opening_balance=natural, opening_balance_date="2026-01-01")

    assert len(ctx.customers.list()) == 3
    assert ctx.customers.get(ctx.customers.list(search="عميل 1")[0]["id"])["balance"] == pytest.approx(50.0)
    assert ctx.reports.party_balances("customer")["net_total"] == pytest.approx(25.0)  # 50 - 25
    invariants.assert_all(ctx)
    invariants.assert_rebuild_idempotent(ctx)