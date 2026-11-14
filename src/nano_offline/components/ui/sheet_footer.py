"""Centred, large sheet/dialog footer used by every bottom sheet.

Primary action is the big CTA; the secondary (cancel/back) sits beside it. Both
are centred with a consistent spacing token so every sheet looks the same.
"""

from __future__ import annotations

import flet as ft

from nano_offline.components.ui.buttons import cta_button, filled_action_button
from nano_offline.core.design.tokens import SPACING, SIZES

__all__ = ["sheet_footer", "dialog_actions"]


def sheet_footer(
    *,
    primary_label: str,
    on_primary,
    icon: str | None = None,
    primary_action: str = "primary",
    cancel_label: str | None = "إلغاء",
    on_cancel=None,
    danger: bool = False,
    primary_expand: bool = False,
) -> ft.Row:
    controls: list[ft.Control] = []
    if cancel_label:
        controls.append(
            ft.OutlinedButton(cancel_label, on_click=on_cancel, height=SIZES["button_lg"])
        )
    controls.append(
        cta_button(
            primary_label,
            icon=icon,
            on_click=on_primary,
            action="danger" if danger else primary_action,
            expand=primary_expand,
        )
    )
    return ft.Row(controls, spacing=SPACING["md"], alignment=ft.MainAxisAlignment.CENTER)


def dialog_actions(
    *,
    confirm_label: str,
    on_confirm,
    cancel_label: str | None = "إلغاء",
    on_cancel=None,
    danger: bool = False,
    icon: str | None = None,
) -> list[ft.Control]:
    controls: list[ft.Control] = []
    if cancel_label:
        controls.append(ft.TextButton(cancel_label, on_click=on_cancel))
    controls.append(
        filled_action_button(
            confirm_label,
            action="danger" if danger else "primary",
            icon=icon,
            on_click=on_confirm,
            height=SIZES["button_md"],
        )
    )
    return controls