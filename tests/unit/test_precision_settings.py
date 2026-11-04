"""Admin decimal-places setting parsing."""

from __future__ import annotations

from nano_offline.core import precision_settings as p


class _Settings:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key, default=None):
        return self._data.get(key, default)


def test_missing_or_auto_means_none():
    assert p.decimals(_Settings(), "price") is None
    assert p.decimals(_Settings({p.SETTING_PRICE: p.AUTO}), "price") is None
    assert p.decimals(_Settings({p.SETTING_PRICE: "auto"}), "price") is None


def test_explicit_value_in_range():
    assert p.decimals(_Settings({p.SETTING_PRICE: "3"}), "price") == 3
    assert p.decimals(_Settings({p.SETTING_QTY: "0"}), "qty") == 0


def test_out_of_range_or_garbage_falls_back_to_auto():
    assert p.decimals(_Settings({p.SETTING_TOTAL: "9"}), "total") is None
    assert p.decimals(_Settings({p.SETTING_TOTAL: "abc"}), "total") is None


def test_unknown_field_is_auto():
    assert p.decimals(_Settings({p.SETTING_PRICE: "2"}), "nope") is None
