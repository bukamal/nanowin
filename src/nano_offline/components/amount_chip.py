"""Emphasised amount blocks — a big number inside a coloured, filled box.

Used for the figures that must grab the eye (POS grand total, amount due,
change, subtotals). Each figure is coloured by its *role* so the same kind of
number reads the same everywhere:

* ``due``    — red, white text   (الإجمالي المطلوب — draws immediate attention)
* ``success``— green, white text (الباقي / المدفوع)
* ``gold``   — amber/gold, white text (المجموع الفرعي / الضريبة)
* ``primary``— brand blue, white text (counts / neutral totals)
* ``neutral``— surface, dark text (a subdued figure)

``amount_chip`` is self-contained (build a one-off). ``amount_chip_frame``
wraps an existing ``ft.Text`` so a caller can keep updating its ``.value``
(POS recalculates the total on every scan).
"""

from __future__ import annotations

from typing import Any

import flet as ft

from nano_offline.core.theme import Colors, Radius, Shadow

__all__ = ["amount_chip", "amount_chip_frame", "chip_colors"]


def chip_colors(role: str) -> tuple[str, str]:
    """Return ``(background, foreground)`` tokens for a figure role."""
    return {
        "due": (Colors.DANGER, Colors.WHITE),
        "success": (Colors.SUCCESS_DARK, Colors.WHITE),
        "gold": (Colors.WARNING_DARK, Colors.WHITE),
        "primary": (Colors.PRIMARY, Colors.WHITE),
        "neutral": (Colors.BACKGROUND_ALT, Colors.TEXT_PRIMARY),
    }.get(role, (Colors.BACKGROUND_ALT, Colors.TEXT_PRIMARY))


def amount_chip_frame(
    value: ft.Text,
    *,
    role: str = "neutral",
    label: str | None = None,
    icon: str | None = None,
    expand: bool = False,
    width: int | None = None,
    padding: int = 14,
) -> ft.Container:
    """Wrap an existing value ``Text`` in the coloured emphasis block."""
    bg, fg = chip_colors(role)
    value.color = fg
    value.weight = ft.FontWeight.BOLD
    row: list[ft.Control] = []
    if icon:
        row.append(ft.Icon(icon, color=fg, size=(value.size or 24) * 0.75))
    row.append(value)
    body: list[ft.Control] = []
    if label:
        body.append(ft.Text(label, size=11, weight=ft.FontWeight.W_600,
                            color=ft.Colors.with_opacity(0.85, fg)))
    body.append(ft.Row(row, spacing=8, alignment=ft.MainAxisAlignment.CENTER,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER))
    return ft.Container(
        ft.Column(body, spacing=2, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=bg,
        border_radius=Radius.MD,
        padding=ft.padding.symmetric(horizontal=padding, vertical=max(8, padding - 4)),
        alignment=ft.alignment.center,
        expand=expand,
        width=width,
        shadow=Shadow.SM,
    )


def amount_chip(
    text: str,
    *,
    role: str = "neutral",
    label: str | None = None,
    icon: str | None = None,
    size: float = 26,
    expand: bool = False,
    width: int | None = None,
    **text_kwargs: Any,
) -> ft.Container:
    """Build a one-off coloured figure block."""
    value = ft.Text(value=str(text), size=size, **text_kwargs)
    return amount_chip_frame(value, role=role, label=label, icon=icon, expand=expand, width=width)
