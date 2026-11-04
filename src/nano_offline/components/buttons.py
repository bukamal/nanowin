"""Shared button widgets for roles that recur across many views.

The app already keeps a clean 4-way split by *widget type* (see
``core/theme.py``'s docstring style): ``FilledButton`` for the primary
action, ``OutlinedButton`` for a secondary action, ``TextButton`` for a
low-emphasis/cancel action, ``IconButton`` for icon-only actions. That part
was already consistent. What drifted was everything *below* that choice —
a handful of roles got re-implemented by hand at each call site, each time
with a slightly different pixel value or a copy-pasted ``ft.ButtonStyle(...)``
block, so the same kind of action ended up looking subtly different from
screen to screen:

- the small header "close (X)" icon button in dialogs/sheets
  (``form_sheet.py``, the definitions sheet, the notifications panel)
- the quantity +/- stepper icon button (invoice editor vs. POS cart)
- the destructive/danger filled button (delete, remove, etc.)
- the single prominent "pay" CTA button on the POS screen

Centralizing each of those here means the *role* now has one look, defined
once, instead of N hand-tuned copies that can quietly diverge again the
next time someone touches one of them. Colors are read from
``core.theme.Colors`` at call time (never cached at import time) so these
stay dark-mode-aware the same way the rest of the app is — see
``core/theme.py``'s module docstring for why that matters.

Usage:
    from nano_offline.components.buttons import danger_button, stepper_icon_button, header_close_button, hero_button
"""

from __future__ import annotations

from typing import Callable

import flet as ft

from nano_offline.core.theme import Colors, IconSize

# --- Unified button geometry (the app-wide "button language") ------------- #
# One primary per screen/sheet; same height + radius for primary/secondary,
# so a row of actions always lines up. Widths are per-role, not per-screen.
BTN_HEIGHT_SM = 36
BTN_HEIGHT_MD = 40
BTN_HEIGHT_LG = 44
BTN_RADIUS = 12
WIDE_WIDTH = 320  # the standard primary CTA width (fits the desktop min window)
BTN_TEXT_SIZE = 15


def _button_height(size: str) -> int:
    return {"sm": BTN_HEIGHT_SM, "md": BTN_HEIGHT_MD, "lg": BTN_HEIGHT_LG}.get(size, BTN_HEIGHT_LG)


def _button_content(icon: str | None, text: str, *, color: str = Colors.WHITE, text_size: int = BTN_TEXT_SIZE):
    """Build a button body with a hidden spinner, so it can show a busy state."""
    ring = ft.ProgressRing(width=16, height=16, stroke_width=2, color=color, visible=False)
    icon_control = ft.Icon(icon, size=18, color=color) if icon else None
    label = ft.Text(text, size=text_size, weight=ft.FontWeight.BOLD, color=color)
    children = [ring]
    if icon_control is not None:
        children.append(icon_control)
    children.append(label)
    row = ft.Row(children, spacing=8, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    row.data = {"ring": ring, "icon": icon_control, "label": label}
    return row


def set_button_busy(button: ft.Control, busy: bool) -> None:
    """Toggle a button's busy state (spinner + disabled) built by the
    ``primary_button``/``hero_button`` factories. Safe to call on any button.
    """
    content = getattr(button, "content", None)
    state = getattr(content, "data", None)
    try:
        button.disabled = busy
        if isinstance(state, dict):
            state["ring"].visible = busy
            if state["icon"] is not None:
                state["icon"].visible = not busy
        button.update()
    except Exception:
        pass


def wire_busy(button: ft.Control, callback: Callable):
    """Wrap ``callback`` so the button shows its busy state while it runs."""
    def handler(e=None):
        set_button_busy(button, True)
        try:
            return callback(e)
        finally:
            set_button_busy(button, False)
    return handler


def primary_button(
    text: str,
    *,
    icon: str | None = None,
    on_click: Callable | None = None,
    width: int | None = WIDE_WIDTH,
    size: str = "lg",
    expand: bool = False,
    **kwargs,
) -> ft.FilledButton:
    """The standard primary action (save/confirm) button.

    Matches the invoice-table save button: 44px tall, 12px radius, bold label,
    a leading icon and a hidden spinner for a busy state. ``width`` defaults to
    the standard wide CTA; pass ``width=None, expand=True`` to fill the parent.
    """
    kwargs.setdefault("height", _button_height(size))
    if expand:
        kwargs["expand"] = True
        kwargs.pop("width", None)
    elif width is not None:
        kwargs.setdefault("width", width)
    button = ft.FilledButton(
        content=_button_content(icon, text),
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor=Colors.PRIMARY,
            color=Colors.WHITE,
            shape=ft.RoundedRectangleBorder(radius=BTN_RADIUS),
            padding=ft.padding.symmetric(horizontal=20),
        ),
        **kwargs,
    )
    return button


def secondary_button(
    text: str,
    *,
    icon: str | None = None,
    on_click: Callable | None = None,
    width: int | None = None,
    size: str = "lg",
    expand: bool = False,
    **kwargs,
) -> ft.OutlinedButton:
    """A secondary action with the same height/radius as the primary."""
    kwargs.setdefault("height", _button_height(size))
    if expand:
        kwargs["expand"] = True
    elif width is not None:
        kwargs.setdefault("width", width)
    return ft.OutlinedButton(
        text,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=BTN_RADIUS),
            padding=ft.padding.symmetric(horizontal=18),
        ),
        **kwargs,
    )


def danger_outline_button(
    text: str,
    *,
    icon: str | None = None,
    on_click: Callable | None = None,
    width: int | None = None,
    size: str = "lg",
    expand: bool = False,
    **kwargs,
) -> ft.OutlinedButton:
    """A destructive secondary action (outlined, danger-colored)."""
    kwargs.setdefault("height", _button_height(size))
    if expand:
        kwargs["expand"] = True
    elif width is not None:
        kwargs.setdefault("width", width)
    return ft.OutlinedButton(
        text,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            color=Colors.DANGER,
            shape=ft.RoundedRectangleBorder(radius=BTN_RADIUS),
            padding=ft.padding.symmetric(horizontal=18),
        ),
        **kwargs,
    )


def footer_actions(
    primary: ft.Control | None = None,
    secondary: list[ft.Control] | None = None,
    *,
    spacing: int = 10,
) -> ft.Row:
    """A consistent sheet/form footer: secondaries, then the primary centred."""
    controls: list[ft.Control] = list(secondary or [])
    if primary is not None:
        controls.append(ft.Container(expand=True))
        controls.append(primary)
        controls.append(ft.Container(expand=True))
    return ft.Row(controls, spacing=spacing, vertical_alignment=ft.CrossAxisAlignment.CENTER)


def danger_button(
    text: str,
    *,
    icon: str | None = None,
    on_click: Callable | None = None,
    width: int | None = None,
    size: str = "lg",
    expand: bool = False,
    **kwargs,
) -> ft.FilledButton:
    """A destructive-action ``FilledButton`` (delete/remove/deactivate/etc.)

    Same shape as every other filled button in the app, just recolored to
    the danger token, so a "حذف" button looks the same whether it's in the
    items list, the security screen, or anywhere else.
    """
    kwargs.setdefault("height", _button_height(size))
    if expand:
        kwargs["expand"] = True
    elif width is not None:
        kwargs.setdefault("width", width)
    return ft.FilledButton(
        text,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE, shape=ft.RoundedRectangleBorder(radius=BTN_RADIUS)),
        **kwargs,
    )


def hero_button(text: str, *, icon: str | None = None, on_click: Callable | None = None, **kwargs) -> ft.FilledButton:
    """The single prominent, full-height primary CTA used at the bottom of
    a checkout/payment flow (POS "دفع"/"بيع").
    """
    kwargs.setdefault("height", 52)
    kwargs.setdefault("width", None)
    return ft.FilledButton(
        content=_button_content(icon, text, text_size=BTN_TEXT_SIZE + 1),
        on_click=on_click,
        style=ft.ButtonStyle(bgcolor=Colors.PRIMARY, color=Colors.WHITE, shape=ft.RoundedRectangleBorder(radius=BTN_RADIUS)),
        **kwargs,
    )


def stepper_icon_button(icon: str, on_click: Callable, *, tooltip: str | None = None, danger: bool = False) -> ft.IconButton:
    """A quantity +/-/remove icon button, sized and colored the same
    wherever a row-level quantity is adjusted (invoice editor line items,
    POS cart rows). Pass one of the ``*_CIRCLE_OUTLINE`` icon glyphs
    (``ft.Icons.ADD_CIRCLE_OUTLINE`` / ``REMOVE_CIRCLE_OUTLINE``) for the
    +/- controls, or ``ft.Icons.CLOSE`` with ``danger=True`` for a
    remove-this-row action.
    """
    return ft.IconButton(
        icon=icon,
        icon_size=IconSize.INLINE,
        tooltip=tooltip,
        icon_color=Colors.DANGER if danger else Colors.PRIMARY,
        on_click=on_click,
    )


def header_close_button(on_click: Callable, *, tooltip: str | None = None) -> ft.IconButton:
    """The small "X" icon button in a dialog/sheet/panel header. Replaces
    three separately hand-tuned copies (form sheets, the definitions
    sheet, the notifications panel) that had drifted to slightly different
    icon sizes over time.
    """
    return ft.IconButton(ft.Icons.CLOSE, icon_size=IconSize.HEADER, icon_color=Colors.TEXT_SECONDARY, tooltip=tooltip, on_click=on_click)


def inline_icon_button(icon: str, on_click: Callable, *, tooltip: str | None = None, color: str | None = None) -> ft.IconButton:
    """A small row-level action icon (edit, etc.) — same size as
    ``stepper_icon_button`` since it plays the same "compact inline
    action" role, just without the quantity-specific coloring default.
    """
    return ft.IconButton(icon, icon_size=IconSize.INLINE, icon_color=color or Colors.TEXT_SECONDARY, tooltip=tooltip, on_click=on_click)


__all__ = [
    "danger_button", "hero_button", "stepper_icon_button", "header_close_button", "inline_icon_button",
    "primary_button", "secondary_button", "danger_outline_button", "footer_actions", "set_button_busy", "wire_busy",
    "BTN_HEIGHT_SM", "BTN_HEIGHT_MD", "BTN_HEIGHT_LG", "BTN_RADIUS", "WIDE_WIDTH",
]
