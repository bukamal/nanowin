"""Unified button factories: geometry, footer layout and busy state."""

from __future__ import annotations

import flet as ft

from nano_offline.components import buttons as B


def test_primary_button_matches_the_save_geometry():
    btn = B.primary_button("حفظ الفاتورة", icon=ft.Icons.SAVE_OUTLINED)
    assert btn.height == B.BTN_HEIGHT_LG
    assert btn.width == B.WIDE_WIDTH
    assert isinstance(btn.content, ft.Row)


def test_primary_expand_drops_the_fixed_width():
    btn = B.primary_button("حفظ", expand=True)
    assert btn.expand is True
    assert btn.width is None


def test_busy_state_toggles_spinner_and_disables():
    btn = B.primary_button("حفظ", icon=ft.Icons.SAVE_OUTLINED)
    state = btn.content.data
    assert state["ring"].visible is False

    B.set_button_busy(btn, True)
    assert btn.disabled is True
    assert state["ring"].visible is True
    assert state["icon"].visible is False

    B.set_button_busy(btn, False)
    assert btn.disabled is False
    assert state["ring"].visible is False
    assert state["icon"].visible is True


def test_footer_actions_centers_the_primary_and_keeps_secondaries():
    primary = B.primary_button("حفظ")
    cancel = B.secondary_button("إلغاء")
    row = B.footer_actions(primary, [cancel])
    assert row.controls[0] is cancel
    assert primary in row.controls


def test_wire_busy_wraps_the_callback():
    calls = []
    btn = B.primary_button("حفظ")
    handler = B.wire_busy(btn, lambda _=None: calls.append(True))
    handler(None)
    assert calls == [True]
    assert btn.disabled is False  # reset after the call
