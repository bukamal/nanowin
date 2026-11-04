"""Toasts mount inside the top-most open modal (they sit above page.overlay)."""

from __future__ import annotations

from types import SimpleNamespace

import flet as ft

from nano_offline.core import toast as toast_mod


def _page():
    return SimpleNamespace(overlay=[])


def test_register_wraps_modal_content_in_a_stack():
    page = _page()
    sheet = ft.BottomSheet(content=ft.Container(ft.Column([])))

    toast_mod.register_modal(page, sheet)

    assert isinstance(sheet.content, ft.Stack)
    assert sheet._nano_toast_stack is sheet.content
    assert page._nano_modal_stack == [sheet]


def test_active_host_is_top_open_modal_else_overlay():
    page = _page()
    sheet = ft.BottomSheet(content=ft.Container())
    toast_mod.register_modal(page, sheet)

    # Not open yet -> fall back to the page overlay.
    assert toast_mod._active_toast_host(page) is page.overlay

    sheet.open = True
    assert toast_mod._active_toast_host(page) is sheet._nano_toast_stack

    sheet.open = False
    assert toast_mod._active_toast_host(page) is page.overlay


def test_unregister_removes_from_stack():
    page = _page()
    sheet = ft.BottomSheet(content=ft.Container())
    toast_mod.register_modal(page, sheet)
    toast_mod.unregister_modal(page, sheet)
    assert page._nano_modal_stack == []


def test_non_modal_and_contentless_controls_are_ignored():
    page = _page()
    toast_mod.register_modal(page, ft.Text("x"))
    toast_mod.register_modal(page, ft.BottomSheet(content=None))
    assert getattr(page, "_nano_modal_stack", None) is None
