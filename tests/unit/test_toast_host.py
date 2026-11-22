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


class _ToastPage:
    def __init__(self):
        self.overlay = []
        self.width = 1280
        self.updated = 0

    def update(self, *a, **k):
        self.updated += 1

    def run_task(self, handler, *args, **kwargs):
        try:
            result = handler(*args, **kwargs)
        except Exception:
            return
        close = getattr(result, "close", None)
        if close is not None:
            close()


def _texts(control):
    out = []
    if isinstance(control, ft.Text):
        out.append(str(control.value))
    for child in getattr(control, "controls", None) or ():
        out.extend(_texts(child))
    inner = getattr(control, "content", None)
    if inner is not None and not isinstance(inner, (str, bytes)):
        out.extend(_texts(inner))
    return out


def test_toast_is_single_slot_with_title_and_detail():
    page = _ToastPage()
    toast_mod.toast(page, "تم الحفظ")
    assert len(page.overlay) == 1
    toast_mod.toast(page, "تم الحفظ", title="فاتورة #12", subtitle="5,473 ل.س")
    assert len(page.overlay) == 1, "a newer toast must replace the previous one"
    texts = _texts(page.overlay[0])
    assert "فاتورة #12" in texts and "5,473 ل.س" in texts


def test_modal_toasts_use_top_placement_and_mount_inside_the_sheet():
    page = _ToastPage()
    sheet = ft.BottomSheet(content=ft.Container())
    toast_mod.register_modal(page, sheet)
    sheet.open = True
    assert toast_mod._prefer_top(page) is True
    toast_mod.toast(page, "تم الحفظ")
    assert len(page.overlay) == 0
    assert page._nano_toast in sheet._nano_toast_stack.controls


def test_page_toasts_default_to_bottom_placement():
    page = _ToastPage()
    assert toast_mod._prefer_top(page) is False
