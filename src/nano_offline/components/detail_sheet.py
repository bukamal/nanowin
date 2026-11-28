"""Unified detail-sheet template.

Every "detail" surface (material, party, voucher, invoice actions, user)
used to hand-roll its own handle + header + body + footer, so they drifted
apart in geometry, header chrome and button order. This module is the single
template they now share:

    handle
    header (gradient icon bubble + title/subtitle + close)
    meta chips                         (fixed)
    summary band (amount chips / KPIs)  (fixed)
    ── scrollable middle: sections (each a title + DataGrid table) ──
    footer actions (danger on the right, primary close on the left)  (fixed)

The top and bottom stay pinned; only the middle scrolls. Tables use the same
shared ``DataGrid`` as the main screens, so they inherit the dark gridlines,
per-column colouring and sharp corners automatically.
"""

from __future__ import annotations

from typing import Iterable

import flet as ft

from nano_offline.components import sheets
from nano_offline.components.data_grid import DataGrid, GridColumn
from nano_offline.core.theme import Colors, Radius

__all__ = [
    "detail_meta",
    "detail_section",
    "detail_table",
    "detail_sheet_content",
    "open_detail_sheet",
]


def detail_meta(icon: str, label: str, value: str) -> ft.Container:
    """A small fixed "chip" (icon + label/value) used in the pinned band."""
    return ft.Container(
        ft.Row(
            [
                ft.Icon(icon, size=15, color=Colors.PRIMARY),
                ft.Column(
                    [
                        ft.Text(label, size=9, color=Colors.TEXT_SECONDARY),
                        ft.Text(str(value), size=11.5, weight=ft.FontWeight.W_600, color=Colors.TEXT_PRIMARY,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ],
                    spacing=0, tight=True,
                ),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.padding.symmetric(horizontal=10, vertical=7),
        bgcolor=Colors.BACKGROUND_ALT,
        border_radius=Radius.SM,
    )


def detail_section(title: str, content: ft.Control, *, subtitle: str | None = None) -> ft.Column:
    """A titled block inside the scrollable middle (usually one table)."""
    heading: list[ft.Control] = [
        ft.Text(title, size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
    ]
    if subtitle:
        heading.append(ft.Text(subtitle, size=10.5, color=Colors.TEXT_SECONDARY))
    return ft.Column(
        [ft.Row(heading, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER), content],
        spacing=6,
    )


def detail_table(
    columns: list[GridColumn],
    rows: Iterable[dict],
    *,
    height: int = 220,
    **grid_kwargs,
) -> ft.Container:
    """A read-only table sized to its own bounded viewport.

    Read-only on purpose (no page attached) so several detail tables never
    fight over the keyboard focus, while still inheriting the shared grid
    styling (gridlines / column tints / dark mode).
    """
    grid = DataGrid(
        columns=columns,
        rows=[dict(r) for r in rows],
        page=None,
        row_height=36,
        header_height=38,
        striped=False,
        viewport_height=height,
        **grid_kwargs,
    )
    return ft.Container(grid, height=height)


def detail_sheet_content(
    *,
    title: str,
    subtitle: str = "",
    icon: str,
    icon_color: str | None = None,
    icon_bg: str | None = None,
    on_close=None,
    trailing: ft.Control | None = None,
    meta: list[ft.Control] | None = None,
    summary: ft.Control | None = None,
    sections: list[ft.Control] | None = None,
    actions: list[ft.Control] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> ft.Container:
    """Build the shared detail-sheet canvas (fixed header/footer, scrolling
    middle). ``sections`` are usually ``detail_section(...)`` blocks."""
    header = sheets.sheet_header(
        title=title,
        subtitle=subtitle,
        icon=icon,
        icon_color=icon_color or Colors.PRIMARY,
        icon_bg=icon_bg or Colors.PRIMARY_BG,
        on_close=on_close or (lambda _=None: None),
        trailing=trailing,
    )
    fixed_top: list[ft.Control] = [ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER), header]
    if meta:
        fixed_top.append(ft.Row(meta, spacing=8, wrap=True, run_spacing=8))
    if summary is not None:
        fixed_top.append(summary)
    fixed_top.append(ft.Divider(height=1, color=Colors.BORDER_ALT))

    scroll_area = ft.Container(
        ft.Column(
            list(sections or []),
            spacing=14,
            scroll=ft.ScrollMode.AUTO,
        ),
        expand=True,
        padding=ft.padding.only(top=2, bottom=2),
    )

    lower: list[ft.Control] = [scroll_area]
    if actions:
        lower.append(ft.Divider(height=1, color=Colors.BORDER_ALT))
        lower.append(
            ft.Row(
                actions,
                spacing=8,
                run_spacing=8,
                wrap=True,
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

    root = ft.Column(
        [*fixed_top, *lower],
        spacing=10,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    return sheets.sheet_frame(root, width=width, height=height)


def open_detail_sheet(page, **kwargs) -> ft.BottomSheet:
    """Create + open a unified detail sheet; returns the sheet (for closing)."""
    sheet, w = sheets.new_desktop_sheet(page)
    h = kwargs.pop("height", None) or sheets.sheet_height(
        page, sheets.DETAIL_HEIGHT_RATIO, min_height=460, max_height=720
    )
    sheet.content = detail_sheet_content(width=w, height=h, **kwargs)
    page.open(sheet)
    return sheet