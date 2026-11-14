"""UI kit buttons: function-coloured variants + a large call-to-action.

Screens pick a *function* (primary/success/danger/warning/info/neutral) and a
size token instead of choosing colours/heights themselves. Thin re-exports of
the legacy factories are kept for gradual migration.
"""

from __future__ import annotations

import flet as ft

from nano_offline.components.buttons import (  # noqa: F401 - re-exported
    primary_button,
    secondary_button,
    danger_button,
    danger_outline_button,
    hero_button,
)
from nano_offline.core.design.tokens import SIZES, action_color, radius_for
from nano_offline.core.theme import Colors

__all__ = [
    "primary_button", "secondary_button", "danger_button", "danger_outline_button", "hero_button",
    "filled_action_button", "cta_button",
]


def _content(text: str, icon: str | None) -> ft.Row:
    items: list[ft.Control] = []
    if icon:
        items.append(ft.Icon(icon, size=20, color=Colors.WHITE))
    items.append(ft.Text(text, size=15, weight=ft.FontWeight.W_600, color=Colors.WHITE))
    return ft.Row(items, spacing=8, tight=True, alignment=ft.MainAxisAlignment.CENTER)


def filled_action_button(
    text: str,
    *,
    action: str = "primary",
    icon: str | None = None,
    on_click=None,
    height: int | None = None,
    expand: bool = False,
) -> ft.FilledButton:
    """A filled button coloured by *function* (see ``action_color``)."""
    return ft.FilledButton(
        content=_content(text, icon),
        on_click=on_click,
        height=height or SIZES["button_lg"],
        expand=expand,
        style=ft.ButtonStyle(
            bgcolor=action_color(action),
            color=Colors.WHITE,
            shape=ft.RoundedRectangleBorder(radius=radius_for("lg")),
        ),
    )


def cta_button(
    text: str,
    *,
    on_click=None,
    icon: str | None = None,
    action: str = "primary",
    expand: bool = True,
) -> ft.FilledButton:
    """A large primary call-to-action (save/pay), sized for touch."""
    return filled_action_button(
        text, action=action, icon=icon, on_click=on_click, height=SIZES["cta"], expand=expand
    )