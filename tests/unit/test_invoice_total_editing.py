"""Editing line totals derives price/quantity; helpers + admin flags."""

from __future__ import annotations

from nano_offline.core import invoice_settings as inv
from nano_offline.core import precision_settings


class _Settings:
    def __init__(self, data=None):
        self._d = dict(data or {})

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def test_quantity_for_total_rounds_down():
    assert inv.quantity_for_total(3000, 15000, 3) == 0.2
    assert inv.quantity_for_total(5000, 15000, 3) == 0.333
    assert inv.quantity_for_total(5000, 15000, 4) == 0.3333
    assert inv.quantity_for_total(15000, 15000, 3) == 1.0


def test_quantity_for_total_guards_invalid():
    assert inv.quantity_for_total(0, 15000, 3) == 0.0
    assert inv.quantity_for_total(5000, 0, 3) == 0.0
    assert inv.quantity_for_total(-1, 15000, 3) == 0.0
    assert inv.quantity_for_total("bad", 15000, 3) == 0.0


def test_edit_total_flags_default_off_and_toggle():
    s = _Settings()
    assert inv.edit_total_changes_price(s) is False
    assert inv.edit_total_changes_qty(s) is False
    s.set(inv.EDIT_TOTAL_PRICE_KEY, "1")
    s.set(inv.EDIT_TOTAL_QTY_KEY, "1")
    assert inv.edit_total_changes_price(s) is True
    assert inv.edit_total_changes_qty(s) is True


def test_total_qty_decimals_defaults_to_three():
    assert inv.total_qty_decimals(_Settings()) == 3
    assert inv.total_qty_decimals(_Settings({precision_settings.SETTING_QTY: "2"})) == 2