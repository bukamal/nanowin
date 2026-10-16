"""Layout decisions for the POS quick-sale screen.

The POS body is a Row -- an independently-scrolling catalog next to a
full-height cart rail -- on desktop, and a Column (catalog over a short
cart panel) on mobile, switched at POS_RAIL_BREAKPOINT. These tests pin
both branches and the resize handler that flips between them, using a
headless fake page (no Flutter, no real window).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import flet as ft

from nano_offline.app_context import AppContext
from nano_offline.views.pos_view import CART_RAIL_WIDTH, POS_RAIL_BREAKPOINT, POSCenter


class _FakePage:
    """Headless stand-in for ft.Page: records updates, no-ops async tasks."""

    def __init__(self, width: int) -> None:
        self.rtl = True
        self.width = width
        self.height = 800
        self.overlay: list = []
        self.tasks: list = []
        self.update_count = 0

    def update(self, *a, **k):  # noqa: ARG002
        self.update_count += 1

    def run_task(self, handler, *args):
        self.tasks.append((handler, args))

    def open(self, *a, **k):  # noqa: ARG002
        pass

    def close(self, *a, **k):  # noqa: ARG002
        pass


def test_breakpoint_matches_shell_ribbon_threshold():
    # The shell's adapt_navigation switches the top ribbon at 900px; POS must
    # switch at the exact same width so the two modes never disagree.
    assert POS_RAIL_BREAKPOINT == 900


def _pos_with_stub_build(width: int):
    """A POSCenter whose heavy _build is replaced by a recorder that keeps
    _pos_wide in sync exactly the way the real _build does."""
    page = _FakePage(width)
    pos = POSCenter(page, object(), ft.Container())
    calls: list = []

    def stub_build():
        calls.append(1)
        pos._pos_wide = bool(page.width and page.width >= POS_RAIL_BREAKPOINT)

    pos._pos_wide = bool(width >= POS_RAIL_BREAKPOINT)
    pos._build = stub_build  # type: ignore[method-assign]
    return pos, page, calls


def test_resize_relayouts_only_on_breakpoint_crossing():
    pos, page, calls = _pos_with_stub_build(1200)
    pos._pos_handle_resize()
    assert calls == [], "same side of the breakpoint must not rebuild"

    page.width = 1300  # still desktop
    pos._pos_handle_resize()
    assert calls == []

    page.width = 899  # crosses down to mobile
    pos._pos_handle_resize()
    assert calls == [1]

    page.width = 400  # still mobile
    pos._pos_handle_resize()
    assert calls == [1]

    page.width = 900  # crosses up to desktop (threshold is inclusive)
    pos._pos_handle_resize()
    assert calls == [1, 1]


def _build_real_pos(width: int) -> POSCenter:
    """Build a real POSCenter against a temp DB so the actual widget tree
    (Row vs Column, rail sizing) can be inspected."""
    db_path = Path(tempfile.mkdtemp()) / "pos_layout.db"
    ctx = AppContext.create(db_path)
    units = ctx.definitions.list_units()
    unit_id = int(units[0]["id"]) if units else ctx.definitions.create_unit("قطعة", "قطعة")
    categories = ctx.definitions.list_categories()
    cat_id = categories[0]["id"] if categories else None
    ctx.items.create(
        name="مادة اختبار",
        category_id=cat_id,
        item_type="مخزون",
        purchase_price=1.0,
        selling_price=2.5,
        quantity=10,
        base_unit_id=unit_id,
    )
    page = _FakePage(width)
    pos = POSCenter(page, ctx, ft.Container())
    pos.show_center()
    return pos


def _body_of(pos: POSCenter):
    # content.content = Stack([main_column, last_scan_overlay, success_overlay])
    # main_column = Column([pos_header, body, bottom_pay_bar])
    stack = pos.content.content
    main_column = stack.controls[0]
    return main_column.controls[1]


def test_wide_screen_uses_side_by_side_cart_rail():
    pos = _build_real_pos(1200)
    assert pos._pos_wide is True
    assert isinstance(_body_of(pos), ft.Row), "desktop body must be a Row"
    assert pos._cart_wrap.width == CART_RAIL_WIDTH
    # The rail must expand to the full body height, not keep the mobile cap.
    assert pos._cart_wrap.content.expand is True
    assert callable(getattr(pos.page, "_nano_pos_relayout", None)), "resize hook installed"


def test_narrow_screen_uses_stacked_body():
    pos = _build_real_pos(400)
    assert pos._pos_wide is False
    assert isinstance(_body_of(pos), ft.Column), "mobile body must be a Column"
    assert pos._cart_wrap.width is None
