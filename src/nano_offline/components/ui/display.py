"""Small shared display primitives: card, badge, skeleton, section title."""

from __future__ import annotations

import flet as ft

from nano_offline.core.design.tokens import SPACING, action_color, radius_for
from nano_offline.core.theme import Colors, Shadow, surface_gradient

__all__ = ["card", "badge", "skeleton", "section_title"]


def card(content: ft.Control, *, padding: int | None = None, shape: str = "standard") -> ft.Container:
    return ft.Container(
        content,
        padding=padding if padding is not None else SPACING["lg"],
        bgcolor=Colors.WHITE,
        gradient=surface_gradient(),
        border=ft.border.all(1, Colors.BORDER_ALT),
        border_radius=radius_for("lg", shape),
        shadow=Shadow.SM,
    )


def badge(text: str, *, action: str = "info") -> ft.Container:
    return ft.Container(
        ft.Text(str(text), size=10, color=Colors.WHITE, weight=ft.FontWeight.W_600),
        padding=ft.padding.symmetric(horizontal=8, vertical=3),
        bgcolor=action_color(action),
        border_radius=10,
    )


def skeleton(width: int | None = 120, height: int = 16) -> ft.Container:
    return ft.Container(
        width=width, height=height,
        bgcolor=Colors.BACKGROUND_ALT,
        border_radius=radius_for("sm"),
    )


def section_title(text: str, *, icon: str | None = None) -> ft.Row:
    items: list[ft.Control] = []
    if icon:
        items.append(ft.Icon(icon, size=18, color=Colors.PRIMARY))
    items.append(ft.Text(text, size=15, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY))
    return ft.Row(items, spacing=SPACING["sm"], vertical_alignment=ft.CrossAxisAlignment.CENTER)