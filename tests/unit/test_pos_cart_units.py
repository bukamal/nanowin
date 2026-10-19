"""Unit-aware POS cart lines (piece vs. carton).

Regression coverage for the carton-barcode overcharge: a code registered
against a multi-piece unit must add ONE of that unit at its own price and
carry the conversion factor to the invoice line -- never N base pieces at
the carton price.
"""

from __future__ import annotations

import flet as ft

from nano_offline.app_context import AppContext
from nano_offline.views.pos_view import POSCenter


class _FakePage:
    def __init__(self) -> None:
        self.rtl = True
        self.width = 1280
        self.height = 800
        self.overlay: list = []
        self.on_keyboard_event = None
        self._nano_pos_relayout = None

    def update(self, *a, **k):  # noqa: ARG002
        pass

    def open(self, *a, **k):  # noqa: ARG002
        pass

    def close(self, *a, **k):  # noqa: ARG002
        pass

    def run_task(self, handler, *args):
        try:
            coro = handler(*args)
            if coro is not None:
                coro.close()
        except Exception:
            pass


def _pos(tmp_path):
    ctx = AppContext.create(str(tmp_path / "pos.db"))
    piece = ctx.definitions.create_unit("قطعة", "ق")
    carton = ctx.definitions.create_unit("كرتونة", "ك")
    item_id = ctx.items.create(
        name="ماء صغير",
        barcode="111",
        selling_price=10,
        purchase_price=6,
        quantity=100,
        base_unit_id=piece,
        item_units=[
            {"unit_id": carton, "conversion_factor": 12, "selling_price": 100, "purchase_price": 60}
        ],
    )
    ctx.items.add_barcode(item_id, "222", unit_id=carton, label="كرتونة")
    pos = POSCenter(_FakePage(), ctx, ft.Container())
    pos.show_center()
    return pos, ctx, item_id, carton


def test_carton_barcode_adds_one_carton_at_its_own_price(tmp_path):
    pos, ctx, item_id, carton = _pos(tmp_path)

    pos._add_by_barcode("222")

    row = next(r for r in pos.cart.values() if int(r["item"]["id"]) == item_id)
    assert row["qty"] == 1.0
    assert row["unit_id"] == carton
    assert row["factor"] == 12.0
    assert row["unit_price"] == 100.0
    assert pos._total_amount() == 100.0


def test_carton_and_piece_are_separate_cart_lines(tmp_path):
    pos, ctx, item_id, carton = _pos(tmp_path)

    pos._add_by_barcode("111")
    pos._add_by_barcode("222")

    assert len(pos.cart_order) == 2
    assert item_id in pos.cart
    carton_row = next(r for r in pos.cart.values() if r.get("unit_id") == carton)
    assert carton_row["qty"] == 1.0
    assert pos._total_amount() == 110.0


def test_carton_checkout_keeps_unit_and_conversion_factor(tmp_path):
    pos, ctx, item_id, carton = _pos(tmp_path)

    pos._add_by_barcode("222")
    pos._do_checkout()

    invoice = ctx.invoices.get_invoice(int(ctx.invoices.list_invoices(limit=1)[0]["id"]))
    line = invoice["lines"][0]
    assert invoice["total"] == 100.0
    assert line["quantity"] == 1.0
    assert line["unit_id"] == carton
    assert line["conversion_factor"] == 12.0
    assert line["quantity_in_base"] == 12.0


def test_price_check_mode_does_not_touch_cart(tmp_path):
    pos, ctx, item_id, carton = _pos(tmp_path)

    pos._price_check_mode = True
    pos._add_by_barcode("222")

    assert not pos.cart_order
    assert pos._total_amount() == 0.0


def test_undo_restores_removed_line(tmp_path):
    pos, ctx, item_id, carton = _pos(tmp_path)

    pos._add_by_barcode("222")
    pos._remember_undo()
    pos.cart.pop(pos.cart_order.pop())

    assert not pos.cart_order
    assert pos._restore_undo() is True
    assert len(pos.cart_order) == 1
    assert pos._total_amount() == 100.0
