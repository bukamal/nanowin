"""Sell-by-amount helpers + admin flags."""

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


def test_quantity_for_amount_rounds_down_to_decimals():
    # 3000 / 15000 = 0.2 exactly
    assert inv.quantity_for_amount(3000, 15000, 3) == 0.2
    # 5000 / 15000 = 0.3333... -> rounds down
    assert inv.quantity_for_amount(5000, 15000, 3) == 0.333
    assert inv.quantity_for_amount(5000, 15000, 4) == 0.3333
    # exact whole unit
    assert inv.quantity_for_amount(15000, 15000, 3) == 1.0


def test_quantity_for_amount_guards_zero_and_negative():
    assert inv.quantity_for_amount(0, 15000, 3) == 0.0
    assert inv.quantity_for_amount(5000, 0, 3) == 0.0
    assert inv.quantity_for_amount(-5, 15000, 3) == 0.0
    assert inv.quantity_for_amount("bad", 15000, 3) == 0.0


def test_sell_by_amount_flags_default_off_and_toggle():
    s = _Settings()
    assert inv.sell_by_amount(s) is False
    assert inv.amount_show_remainder(s) is False
    s.set(inv.SELL_BY_AMOUNT_KEY, "1")
    s.set(inv.AMOUNT_SHOW_REMAINDER_KEY, "1")
    assert inv.sell_by_amount(s) is True
    assert inv.amount_show_remainder(s) is True


def test_amount_qty_decimals_defaults_to_three_then_follows_setting():
    assert inv.amount_qty_decimals(_Settings()) == 3
    assert inv.amount_qty_decimals(_Settings({precision_settings.SETTING_QTY: "2"})) == 2