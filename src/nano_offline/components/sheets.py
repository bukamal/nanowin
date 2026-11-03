"""Shared desktop geometry for Flet bottom sheets.

Every sheet in the app used to size itself ad hoc, so some came out as narrow
phone-style modals on a wide desktop window. This module centralises the two
rules the good sheets (invoices / items / notifications) already followed:

* **Width** -- a sheet spans the window minus a small gutter, optionally
  capped, and is pinned to that width with ``size_constraints`` (so the
  ``BottomSheet`` never shrinks to its content).
* **Height** -- table sheets take most of the window (a grid needs room and
  scrolls internally); detail sheets take a comfortable fixed share; form
  sheets stay content-driven (they grow with their fields / the keyboard).

The helpers only compute sizes and decorate the sheet; each view still owns
its own header/body/footer so a *table* sheet can pin a footer under an
expanding grid while a *detail* sheet scrolls its body under a pinned header.
"""

from __future__ import annotations

import flet as ft

from nano_offline.core.theme import Colors, Radius, Shadow

TABLE_HEIGHT_RATIO = 0.88
DETAIL_HEIGHT_RATIO = 0.74

_MIN_WIDTH = 360
_DEFAULT_WINDOW_W = 1280
_DEFAULT_WINDOW_H = 800


def sheet_width(page, *, cap: int | None = 1200, gutter: int = 32) -> int:
    try:
        window_w = int(getattr(page, "width", None) or _DEFAULT_WINDOW_W)
    except Exception:
        window_w = _DEFAULT_WINDOW_W
    width = max(_MIN_WIDTH, window_w - gutter)
    if cap:
        width = min(width, cap)
    return width


def sheet_height(page, ratio: float, *, min_height: int = 420, max_height: int | None = None) -> int:
    try:
        window_h = int(getattr(page, "height", None) or _DEFAULT_WINDOW_H)
    except Exception:
        window_h = _DEFAULT_WINDOW_H
    height = max(min_height, int(window_h * ratio))
    if max_height:
        height = min(height, max_height)
    return height


def constrain_width(sheet: ft.BottomSheet, width: int) -> None:
    """Pin a ``BottomSheet`` to ``width`` so it can never shrink-wrap narrow."""
    try:
        sheet.size_constraints = ft.BoxConstraints(min_width=width, max_width=width)
    except Exception:
        pass


def new_desktop_sheet(page, *, cap: int | None = 1200) -> tuple[ft.BottomSheet, int]:
    """A scroll-controlled, width-pinned sheet plus its computed width.

    Callers set ``sheet.content`` to a frame built with :func:`sheet_frame`
    (or their own Container) and open it as usual.
    """
    width = sheet_width(page, cap=cap)
    sheet = ft.BottomSheet(
        content=ft.Container(),
        is_scroll_controlled=True,
        enable_drag=True,
        maintain_bottom_view_insets_padding=True,
    )
    constrain_width(sheet, width)
    return sheet, width


def sheet_frame(
    body: ft.Control,
    *,
    width: int | None = None,
    height: int | None = None,
    padding: ft.Padding | None = None,
) -> ft.Container:
    """The rounded white canvas every desktop sheet content sits on."""
    return ft.Container(
        body,
        width=width,
        height=height,
        padding=padding or ft.padding.only(left=18, right=18, top=12, bottom=18),
        bgcolor=Colors.WHITE,
        border_radius=ft.border_radius.only(top_left=28, top_right=28),
        shadow=Shadow.LG,
        rtl=True,
    )


def sheet_handle() -> ft.Container:
    return ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center)


def sheet_header(
    *,
    title: str,
    subtitle: str = "",
    icon: str,
    icon_color: str = Colors.PRIMARY,
    icon_bg: str = Colors.PRIMARY_BG,
    on_close,
    trailing: ft.Control | None = None,
) -> ft.Row:
    """Drag-agnostic header row: icon bubble + title/subtitle + optional
    trailing controls + close button. Used by every unified sheet."""
    from nano_offline.components.buttons import header_close_button

    left = ft.Column(
        [
            ft.Text(title, size=17, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
            ft.Text(subtitle, size=11, color=Colors.TEXT_SECONDARY) if subtitle else ft.Container(height=0),
        ],
        spacing=1,
        expand=True,
        tight=True,
    )
    controls: list[ft.Control] = [
        ft.Container(
            ft.Icon(icon, color=icon_color, size=22),
            width=44, height=44, alignment=ft.alignment.center,
            bgcolor=icon_bg, border_radius=14,
        ),
        left,
    ]
    if trailing is not None:
        controls.append(trailing)
    controls.append(header_close_button(on_close))
    return ft.Row(controls, spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)


def table_body(body: ft.Control) -> ft.Container:
    """Frame an expanding grid/table so it scrolls inside its own viewport."""
    return ft.Container(
        body,
        expand=True,
        border=ft.border.all(1, Colors.BORDER),
        border_radius=Radius.MD,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
    )


__all__ = [
    "TABLE_HEIGHT_RATIO", "DETAIL_HEIGHT_RATIO",
    "sheet_width", "sheet_height", "constrain_width", "new_desktop_sheet",
    "sheet_frame", "sheet_handle", "sheet_header", "table_body",
]
