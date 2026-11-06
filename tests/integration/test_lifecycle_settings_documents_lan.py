"""Extended lifecycle: settings effects across modules + document rendering +
LAN voucher parity."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import (
    currency,
    font_settings,
    grid_settings,
    invoice_settings,
    precision_settings,
    sound_settings,
    theme_settings,
)
from nano_offline.services.invoice_service import InvoiceLineInput
from nano_offline.services.lan_api_server import LanApiServer
from nano_offline.services.remote_client import RemoteContext

PASSWORD = "StrongPass1"


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-settings-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.auth.login("admin", PASSWORD)
    return ctx, tmp


def _seed(ctx: AppContext):
    unit = ctx.definitions.create_unit("قطعة", "ق")
    customer = ctx.customers.create("عميل")
    supplier = ctx.suppliers.create("مورد")
    item = ctx.items.create(
        name="مادة", purchase_price=2.0, selling_price=3.0, quantity=10.0,
        base_unit_id=unit, barcode="S-1",
    )
    ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
    ctx.invoices.create_invoice(
        invoice_type="purchase", supplier_id=supplier,
        lines=[InvoiceLineInput(description="x", item_id=item, quantity=5, unit_price=2.5, unit_id=unit)],
    )
    sale = ctx.invoices.create_invoice(
        invoice_type="sale", customer_id=customer, paid_amount=5.0,
        lines=[InvoiceLineInput(description="x", item_id=item, quantity=3, unit_price=3.0, unit_id=unit)],
    )
    voucher = ctx.payments.create_voucher(voucher_type="receipt", amount=4.0, customer_id=customer)
    ctx.expenses.create_expense(amount=1.0, description="كهرباء")
    return unit, customer, supplier, item, sale, voucher


def test_settings_affect_formatting_flags_and_theme():
    ctx, tmp = _ctx()
    try:
        # currency
        currency.set_display_currency(ctx.settings, "SYP")
        currency.set_exchange_rate(ctx.settings, 12000)
        assert currency.format_amount(1.0, ctx.settings) == "12,000 ل.س"
        currency.set_display_currency(ctx.settings, "USD")
        assert currency.format_amount(1.0, ctx.settings) == "1.00 $"

        # precision (price / qty / total)
        ctx.settings.set_many({
            precision_settings.SETTING_PRICE: "3",
            precision_settings.SETTING_QTY: "2",
            precision_settings.SETTING_TOTAL: "0",
        })
        assert currency.format_amount(1.5, ctx.settings, field="price") == "1.500 $"
        assert currency.format_qty(2.0, ctx.settings) == "2.00"
        assert currency.format_amount(126.0, ctx.settings) == "126 $"

        # invoice numbering + flags
        ctx.settings.set_many({
            invoice_settings.NUMBER_PREFIX_KEY: "INV-",
            invoice_settings.NUMBER_PADDING_KEY: "5",
            invoice_settings.ALLOW_NEGATIVE_STOCK_KEY: "1",
        })
        assert invoice_settings.format_invoice_number(7, ctx.settings) == "INV-00007"
        assert invoice_settings.allow_negative_stock(ctx.settings) is True

        # grid fit-to-width
        assert grid_settings.fit_width(ctx.settings) is True
        ctx.settings.set(grid_settings.FIT_WIDTH_KEY, "0")
        assert grid_settings.fit_width(ctx.settings) is False

        # sound
        assert sound_settings.sound_enabled(ctx.settings) is True
        assert sound_settings.kind_enabled(ctx.settings, "cell") is True
        ctx.settings.set(sound_settings.ENABLED_KEY, "0")
        assert sound_settings.sound_enabled(ctx.settings) is False

        # theme + font
        theme_settings.set_mode_preference(ctx.settings, "dark")
        assert theme_settings.resolve_effective_mode(ctx.settings, system_is_dark=False) == "dark"
        ctx.settings.set_many({
            font_settings.SETTING_FAMILY: "Tahoma",
            font_settings.SETTING_SIZE: "17",
        })
        assert font_settings.family(ctx.settings) == "Tahoma"
        assert font_settings.font_size(ctx.settings) == 17
        assert font_settings.ui_scale_factor(ctx.settings) == pytest.approx(17 / 13)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_documents_render_for_all_types():
    ctx, tmp = _ctx()
    try:
        currency.set_display_currency(ctx.settings, "SYP")
        currency.set_exchange_rate(ctx.settings, 12000)
        unit, customer, _supplier, item, sale, voucher = _seed(ctx)

        html = ctx.documents.invoice_html(sale)
        assert html and "ل.س" in html

        voucher_data = ctx.payments.get_voucher(voucher)
        assert ctx.documents.voucher_html(voucher_data)

        assert ctx.documents.statement_html("customer", customer)

        labels = ctx.documents.barcode_labels_html(
            [{"name": "مادة", "barcode": "S-1", "price": 3.0}], columns=2,
        )
        assert labels and "svg" in labels.lower()

        card = ctx.documents.item_card_html(
            payload="payload-1", name="مادة", barcode="S-1", selling_price=3.0,
            purchase_price=2.0, unit="قطعة", category="مواد",
        )
        assert card

        party = ctx.documents.party_card_html("customer", ctx.customers.activity_summary(customer))
        assert party

        pnl = ctx.documents.pnl_report_html(ctx.reports.income_statement(), date_from=None, date_to=None)
        assert pnl

        prof = ctx.documents.profitability_report_html(
            invoices=ctx.reports.invoice_profitability(),
            items=ctx.reports.item_profitability(),
            top=[],
            date_from=None,
            date_to=None,
        )
        assert prof
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_lan_voucher_write_parity():
    ctx, tmp = _ctx()
    try:
        customer = ctx.customers.create("عميل")
        server = LanApiServer(ctx, host="127.0.0.1", port=0)
        server.start()
        try:
            remote = RemoteContext(f"http://127.0.0.1:{server.port}")
            assert remote.login("admin", PASSWORD)["role"] == "admin"

            before = len(ctx.payments.list_vouchers())
            remote.payments.create_voucher(voucher_type="receipt", amount=12.0, customer_id=customer)
            assert len(ctx.payments.list_vouchers()) == before + 1
            assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(12.0, abs=1e-6)

            # Read parity for the dashboard after the remote write.
            remote_summary = remote.dashboard.summary()
            local_summary = ctx.dashboard.summary()
            for key in ("sales", "purchases", "cash", "inventory_value"):
                assert float(remote_summary[key]) == pytest.approx(float(local_summary[key]), abs=1e-6)
        finally:
            server.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)