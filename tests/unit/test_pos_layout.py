"""Layout decisions for the POS quick-sale screen.

The POS body is always a Row -- an independently-scrolling catalog next to a
full-height cart rail -- at every window width. The app is desktop-only, so
there is no stacked mobile body and no breakpoint to switch between. These
tests pin the single layout and the resize handler, using a headless fake
page (no Flutter, no real window).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import flet as ft

from nano_offline.app_context import AppContext
from nano_offline.views.pos_view import CART_RAIL_WIDTH, POSCenter


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


def _pos_with_stub_build(width: int):
    """A POSCenter whose heavy _build is replaced by a recorder that keeps
    _pos_wide in sync exactly the way the real _build does."""
    page = _FakePage(width)
    pos = POSCenter(page, object(), ft.Container())
    calls: list = []

    def stub_build():
        calls.append(1)
        pos._pos_wide = True

    pos._pos_wide = True
    pos._build = stub_build  # type: ignore[method-assign]
    return pos, page, calls


def test_resize_never_rebuilds_desktop_layout():
    pos, page, calls = _pos_with_stub_build(1200)
    for width in (1300, 899, 400, 900, 2400):
        page.width = width
        pos._pos_handle_resize()
    assert calls == [], "desktop layout never changes with width"


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
    # main_column = Column([pos_header, body])
    stack = pos.content.content
    main_column = stack.controls[0]
    return main_column.controls[1]


def test_body_is_side_by_side_cart_rail_at_every_width():
    for width in (400, 900, 1200):
        pos = _build_real_pos(width)
        assert pos._pos_wide is True
        assert isinstance(_body_of(pos), ft.Row), "desktop body must be a Row"
        assert pos._cart_wrap.width == CART_RAIL_WIDTH
        # The rail must expand to the full body height.
        assert pos._cart_wrap.content.expand is True
        assert callable(getattr(pos.page, "_nano_pos_relayout", None)), "resize hook installed"


def test_body_has_vertical_rail_catalog_and_cart():
    pos = _build_real_pos(1400)
    body = _body_of(pos)
    # [vertical categories/best-sellers rail, catalog, cart rail]
    assert isinstance(body, ft.Row)
    assert len(body.controls) == 3
    rail, _catalog, cart = body.controls
    assert rail.width == 210, "categories/best-sellers live in a fixed vertical rail"
    # The running total + pay action are pinned as the cart's own footer
    # (header row, held row, scrolling lines, footer) instead of a
    # full-width bar under the whole screen.
    cart_pane_column = cart.content.content
    assert len(cart_pane_column.controls) == 4


def test_pos_header_has_no_voice_or_mode_toggle():
    pos = _build_real_pos(1200)
    pos_header = pos.content.content.controls[0].controls[0]
    # exit button + title/summary column -- the mic and the flow/full toggle
    # are gone, so POS is voice-free and always shows the full toolset.
    assert len(pos_header.content.controls) == 2
    assert not hasattr(pos, "apply_voice_command")
    assert not hasattr(pos, "voice_add_by_name")


def test_recent_items_stay_collapsed_until_requested():
    pos = _build_real_pos(1200)
    item_id = next(iter(pos.item_map))
    pos.recent_item_ids = [item_id]
    pos._render_recent()

    assert pos._recent_toggle.visible is True
    assert pos._recent_row.visible is False

    pos._recent_toggle.on_click(None)
    assert pos._recent_row.visible is True
