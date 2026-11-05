"""Print-profile preferences: per-surface layout/width with global fallback."""

from __future__ import annotations

from nano_offline.components import print_profile as pp


class _Settings:
    def __init__(self, data=None):
        self._d = dict(data or {})

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def test_defaults_when_nothing_saved():
    assert pp.saved_print_profile(_Settings()) == (pp.DEFAULT_PRINT_LAYOUT, 80)


def test_surface_specific_value_wins_and_global_is_fallback():
    s = _Settings({
        pp.PRINT_LAYOUT_KEY: "roll",
        "print_layout_invoice": "sheet",
        "print_roll_width_invoice": "80",
    })
    assert pp.saved_print_profile(s, "invoice") == ("sheet", 80)
    # A surface with no override falls back to the global choice.
    assert pp.saved_print_profile(s, "pos") == ("roll", 80)


def test_value_parsers_reject_garbage():
    assert pp._layout_value("sheet") == "sheet"
    assert pp._layout_value("nope") is None
    assert pp._layout_value(None) is None
    assert pp._width_value("80") == "80"
    assert pp._width_value("999") is None


def test_save_print_layout_persists_only_that_surface():
    s = _Settings()
    pp.save_print_layout(s, "pos", "roll")
    assert s.get("print_layout_pos") == "roll"
    assert s.get(pp.PRINT_LAYOUT_KEY) is None
    # An invalid layout is coerced to the default.
    pp.save_print_layout(s, "pos", "bogus")
    assert s.get("print_layout_pos") == pp.DEFAULT_PRINT_LAYOUT


def test_test_page_html_is_rtl_and_non_empty():
    html = pp._test_page_html()
    assert "dir=\"rtl\"" in html and "مادة" in html