"""Overselling policy, POS stock-visibility settings, and the single-page
quick-sale payment sheet.

The negative-stock permission is snapshotted on each invoice so disabling
the admin setting later cannot break a rebuild of historical oversold sales.
"""

from __future__ import annotations

from types import SimpleNamespace

import flet as ft
import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import invoice_settings, pos_settings
from nano_offline.services.invoice_service import InvoiceLineInput
from nano_offline.views.dashboard_view import DASHBOARD_KPIS_VISIBLE_KEY, DashboardCenter
from nano_offline.views.pos_view import POSCenter


class _FakePage:
    def __init__(self) -> None:
        self.rtl = True
        self.width = 1280
        self.height = 800
        self.overlay: list = []
        self.on_keyboard_event = None
        self._nano_pos_relayout = None
        self.tasks: list = []

    def update(self, *a, **k):  # noqa: ARG002
        pass

    def open(self, *a, **k):  # noqa: ARG002
        pass

    def close(self, *a, **k):  # noqa: ARG002
        pass

    def run_task(self, handler, *args, **kwargs):
        self.tasks.append((handler, args))
        try:
            coro = handler(*args, **kwargs)
            if coro is not None:
                coro.close()
        except Exception:
            pass


def _ctx_with_item(tmp_path, *, quantity: float = 1.0):
    ctx = AppContext.create(str(tmp_path / "neg.db"))
    units = ctx.definitions.list_units()
    unit_id = int(units[0]["id"]) if units else ctx.definitions.create_unit("قطعة", "قطعة")
    item_id = ctx.items.create(
        name="مادة اختبار",
        item_type="مخزون",
        purchase_price=5.0,
        selling_price=10.0,
        quantity=quantity,
        base_unit_id=unit_id,
    )
    return ctx, item_id


def _sale_line(item_id: int, qty: float = 2.0) -> InvoiceLineInput:
    return InvoiceLineInput(description="مادة اختبار", item_id=item_id, quantity=qty, unit_price=10.0)


def test_oversell_is_rejected_by_default_and_preserves_stock(tmp_path):
    ctx, item_id = _ctx_with_item(tmp_path, quantity=1.0)

    with pytest.raises(ValueError, match="المخزون غير كافٍ"):
        ctx.invoices.create_invoice(
            invoice_type="sale",
            lines=[_sale_line(item_id)],
            paid_amount=20.0,
        )

    assert ctx.items.get(item_id)["quantity"] == 1.0
    assert ctx.invoices.list_invoices(limit=10) == []


def test_oversell_snapshot_allows_negative_stock_and_survives_rebuild(tmp_path):
    ctx, item_id = _ctx_with_item(tmp_path, quantity=1.0)

    invoice_id = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[_sale_line(item_id)],
        paid_amount=20.0,
        allow_negative_stock=True,
    )

    invoice = ctx.invoices.get_invoice(invoice_id)
    assert invoice["allow_negative_stock"] == 1
    assert ctx.items.get(item_id)["quantity"] == -1.0
    assert invoice["lines"][0]["cost_amount"] == 10.0

    # Turning the admin policy off must not invalidate the historical sale.
    ctx.settings.set(pos_settings.ALLOW_NEGATIVE_STOCK_KEY, "0")
    ctx.invoices.rebuild_derived_state()
    assert ctx.items.get(item_id)["quantity"] == -1.0


def test_editing_an_oversold_invoice_preserves_snapshot_when_flag_omitted(tmp_path):
    ctx, item_id = _ctx_with_item(tmp_path, quantity=1.0)
    invoice_id = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[_sale_line(item_id)],
        paid_amount=20.0,
        allow_negative_stock=True,
    )

    ctx.invoices.update_invoice(
        invoice_id,
        invoice_type="sale",
        lines=[_sale_line(item_id)],
        paid_amount=20.0,
    )

    assert ctx.invoices.get_invoice(invoice_id)["allow_negative_stock"] == 1
    assert ctx.items.get(item_id)["quantity"] == -1.0


def test_pos_settings_defaults_and_flags(tmp_path):
    ctx, _item_id = _ctx_with_item(tmp_path)

    assert pos_settings.auto_print_default(ctx.settings) is True
    assert pos_settings.allow_negative_stock(ctx.settings) is False
    assert pos_settings.show_stock_in_cart(ctx.settings) is False
    assert pos_settings.f12_print_enabled(ctx.settings) is True

    ctx.settings.set_many({
        pos_settings.ALLOW_NEGATIVE_STOCK_KEY: "1",
        pos_settings.SHOW_STOCK_IN_CART_KEY: "1",
        pos_settings.F12_PRINT_ENABLED_KEY: "0",
    })
    assert pos_settings.allow_negative_stock(ctx.settings) is True
    assert pos_settings.show_stock_in_cart(ctx.settings) is True
    assert pos_settings.f12_print_enabled(ctx.settings) is False


def test_invoice_stock_settings_defaults(tmp_path):
    ctx, _item_id = _ctx_with_item(tmp_path)

    assert invoice_settings.allow_negative_stock(ctx.settings) is False
    assert invoice_settings.show_stock_badge(ctx.settings) is True


def _texts(control) -> list[str]:
    found: list[str] = []
    if control is None:
        return found
    for attr in ("text", "value"):
        text = getattr(control, attr, None)
        if isinstance(text, str) and text:
            found.append(text)
    for attr in ("content",):
        found.extend(_texts(getattr(control, attr, None)))
    for child in getattr(control, "controls", None) or []:
        found.extend(_texts(child))
    return found


def _pos(tmp_path, *, allow_negative: bool = False) -> tuple[POSCenter, AppContext, int]:
    ctx, item_id = _ctx_with_item(tmp_path, quantity=1.0)
    if allow_negative:
        ctx.settings.set(pos_settings.ALLOW_NEGATIVE_STOCK_KEY, "1")
    page = _FakePage()
    pos = POSCenter(page, ctx, ft.Container(), native_files=object())
    pos.show_center()
    return pos, ctx, item_id


def test_pos_checkout_oversells_when_setting_enabled(tmp_path):
    pos, ctx, item_id = _pos(tmp_path, allow_negative=True)
    pos._add_item(item_id, 2.0)

    assert pos._do_checkout() is True
    assert ctx.items.get(item_id)["quantity"] == -1.0
    invoice = ctx.invoices.get_invoice(pos._last_invoice_id)
    assert invoice["allow_negative_stock"] == 1


def test_payment_sheet_is_single_page_with_complete_and_print(tmp_path):
    pos, _ctx, item_id = _pos(tmp_path)
    pos._add_item(item_id, 1.0)
    pos._checkout_btn.on_click(None)

    labels = _texts(pos._payment_sheet.content)
    assert "إتمام وطباعة" in labels
    assert "التالي" not in labels
    assert "رجوع" not in labels


def test_cart_hides_on_hand_quantity_by_default(tmp_path):
    pos, ctx, item_id = _pos(tmp_path)
    pos._add_item(item_id, 2.0)

    texts = _texts(pos._cart_wrap.content)
    assert not any("المتوفر" in text for text in texts)

    ctx.settings.set(pos_settings.SHOW_STOCK_IN_CART_KEY, "1")
    pos._refresh_cart()
    texts = _texts(pos._cart_wrap.content)
    assert any("المتوفر" in text for text in texts)


def test_f12_prints_the_last_completed_sale(tmp_path):
    pos, _ctx, item_id = _pos(tmp_path)
    pos._add_item(item_id, 1.0)
    assert pos._do_checkout() is True
    invoice_id = pos._last_invoice_id

    calls: list[tuple[int, bool]] = []

    def fake_print(target_id: int, *, interactive: bool = True) -> None:
        calls.append((target_id, interactive))

    pos._print_receipt = fake_print
    pos.page.on_keyboard_event(SimpleNamespace(key="F12", ctrl=False, meta=False))

    # F12 prints the last sale silently, without the paper-profile dialog.
    assert calls == [(invoice_id, False)]


def test_f12_shortcut_can_be_disabled_from_settings(tmp_path):
    pos, ctx, item_id = _pos(tmp_path)
    ctx.settings.set(pos_settings.F12_PRINT_ENABLED_KEY, "0")
    pos._add_item(item_id, 1.0)
    assert pos._do_checkout() is True

    calls: list[tuple[int, bool]] = []
    pos._print_receipt = lambda target_id, *, interactive=True: calls.append((target_id, interactive))
    pos.page.on_keyboard_event(SimpleNamespace(key="F12", ctrl=False, meta=False))

    assert calls == []


def test_dashboard_kpi_preference_is_per_user_and_parses_flags():
    ctx = SimpleNamespace(auth=SimpleNamespace(current=lambda: SimpleNamespace(id=7)))
    dashboard = DashboardCenter(
        _FakePage(),
        ctx,
        ft.Container(),
        on_navigate=lambda _route: None,
        on_open_sale=lambda: None,
        on_open_purchase=lambda: None,
    )

    assert dashboard._kpi_pref_key() == f"{DASHBOARD_KPIS_VISIBLE_KEY}.7"
    assert dashboard._flag_enabled("1") is True
    assert dashboard._flag_enabled("true") is True
    assert dashboard._flag_enabled("0") is False
    assert dashboard._flag_enabled("false") is False
