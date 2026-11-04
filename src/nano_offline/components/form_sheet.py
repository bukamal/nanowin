from __future__ import annotations

from typing import Callable, Iterable

import flet as ft

from nano_offline.components.buttons import (
    footer_actions,
    header_close_button,
    primary_button,
    secondary_button,
    wire_busy,
)
from nano_offline.core.theme import Colors, Shadow


_LAYOUT_TYPES = tuple(
    t for t in (ft.Row, ft.Column, ft.Container, getattr(ft, "ResponsiveRow", None)) if t is not None
)


def _iter_text_fields(control):
    """Yield the text fields inside a control (depth-first, reading order).

    Only *plain* layout containers are traversed; composite widgets (e.g.
    ``SearchSelect``, a ``Column`` subclass with its own internal field) are
    treated as leaves so their keys keep their own behaviour.
    """
    if isinstance(control, ft.TextField):
        yield control
        return
    if type(control) not in _LAYOUT_TYPES:
        return
    for child in getattr(control, "controls", None) or ():
        yield from _iter_text_fields(child)
    content = getattr(control, "content", None)
    if content is not None:
        yield from _iter_text_fields(content)


def wire_enter_navigation(fields: Iterable[ft.Control], on_last_submit: Callable) -> None:
    """Make Enter step through a form's text fields.

    Enter on a field focuses the next text field; Enter on the last one runs
    ``on_last_submit(None)`` (save/confirm). A field that already declares its
    own ``on_submit`` is left untouched. This keeps keyboard entry uniform with
    the editable grids: advance one field at a time, commit on the last.
    """
    text_fields = [field for control in fields for field in _iter_text_fields(control)]
    for index, field in enumerate(text_fields):
        if getattr(field, "on_submit", None) is not None:
            continue
        if index + 1 < len(text_fields):
            target = text_fields[index + 1]
            field.on_submit = lambda _e, target=target: target.focus()
        else:
            field.on_submit = lambda _e, callback=on_last_submit: callback(None)


def render_form_sheet(
    page: ft.Page,
    sheet: ft.BottomSheet,
    *,
    title: str,
    fields: Iterable[ft.Control],
    on_close: Callable,
    on_save: Callable,
    save_label: str = "حفظ",
    save_icon: str | None = None,
) -> None:
    """Fill an already-created ``ft.BottomSheet`` with the standard add/edit
    form chrome: drag handle, header (title + close), the form fields, and
    a Cancel/Save footer.

    No manual height math here on purpose. This used to compute an
    explicit pixel height for the outer Container from ``page.height``
    (see git history / the old "Root cause" comment this replaced) to
    work around Flutter's modal bottom sheet shrink-wrapping to a fixed
    ~9/16-screen ceiling -- but that fixed height is exactly what stopped
    the sheet from being able to grow *above* the on-screen keyboard, so
    a field near the bottom of a tall form would still end up hidden
    underneath it.

    The actual fix -- and the one Flutter/Flet officially recommend for
    this -- is ``new_form_sheet()``'s ``is_scroll_controlled=True``
    (removes that shrink-wrap ceiling entirely, so the sheet is free to
    take whatever height it needs, up to the full screen) together with
    ``maintain_bottom_view_insets_padding=True`` (pads the sheet by the
    keyboard's height so the whole thing rises above it automatically).
    With both set, a single scrollable ``Column`` holding header + fields
    + footer is all that's needed: whichever field the user taps gets
    auto-scrolled into view above the keyboard by Flutter itself, with no
    pixel math to keep in sync as forms change. Same shape as the
    security_view.py sheets, which already worked this way.
    """
    save_icon = save_icon or ft.Icons.SAVE_OUTLINED
    field_controls = list(fields)

    save_button = primary_button(save_label, icon=save_icon)

    def submit(_=None):
        on_save(_)

    save_button.on_click = wire_busy(save_button, submit)
    cancel_button = secondary_button("إلغاء", icon=ft.Icons.CLOSE, on_click=on_close)
    # Enter advances field-by-field and saves from the last field.
    wire_enter_navigation(field_controls, save_button.on_click)

    # Every form sheet shares the desktop width so a form never opens as a
    # narrow phone modal on a wide window. Height stays content-driven (the
    # sheet grows with its fields / the on-screen keyboard).
    from nano_offline.components.sheets import constrain_width, sheet_width

    width = sheet_width(page)
    constrain_width(sheet, width)

    sheet.content = ft.Container(
        ft.Column(
            [
                ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                ft.Row(
                    [
                        ft.Text(title, size=18, weight=ft.FontWeight.BOLD, expand=True),
                        header_close_button(on_close),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Column(field_controls, spacing=9),
                footer_actions(save_button, [cancel_button]),
            ],
            spacing=16, tight=True, scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
        width=width,
        padding=ft.padding.only(left=18, right=18, top=12, bottom=20),
        bgcolor=Colors.WHITE,
        border_radius=ft.border_radius.only(top_left=28, top_right=28),
        shadow=Shadow.LG,
    )


def new_form_sheet() -> ft.BottomSheet:
    """A bare ``ft.BottomSheet`` pre-configured the same way every form
    sheet in the app needs: draggable, allowed to grow past Flutter's
    default ~9/16-screen shrink-wrap cap (``is_scroll_controlled``), and
    padded to rise above the on-screen keyboard
    (``maintain_bottom_view_insets_padding``) so the field being typed
    into is never hidden behind it. Fill its ``content`` via
    :func:`render_form_sheet` before opening it.
    """
    return ft.BottomSheet(
        content=ft.Container(),
        is_scroll_controlled=True,
        enable_drag=True,
        maintain_bottom_view_insets_padding=True,
    )


__all__ = ["render_form_sheet", "new_form_sheet", "wire_enter_navigation"]
