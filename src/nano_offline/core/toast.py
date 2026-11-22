"""A modern floating toast notification -- replacement for ``ft.SnackBar``.

Flet's built-in ``SnackBar`` sits flush against the bottom edge and is hard
to style consistently. :func:`toast` instead floats its own card in
``page.overlay`` (or inside the top-most open sheet/dialog -- see
``register_modal``). One reusable slot lives on the page object itself, so a
new toast replaces the previous one instead of stacking duplicates.

Visual language
---------------
* a colored accent bar + a gradient icon chip (kind aware),
* an optional bold title with a secondary detail line,
* a slim auto-dismiss progress bar (pauses while the pointer is over it),
* a spring-like slide/scale entrance and a quick slide-out,
* placement chosen by context: bottom-centre normally, top-centre inside an
  open sheet or on full-screen surfaces (POS) so it never covers the
  footer actions or the checkout bar.

Usage (drop-in replacement for every existing ``notify``/``_notify`` helper):

    from nano_offline.core.toast import toast
    toast(self.page, "تم الحفظ")                      # kind auto-detected
    toast(self.page, "تعذّر الاتصال", kind="error")     # explicit override
    toast(self.page, "تم الحفظ", title="فاتورة #123", subtitle="5,473 ل.س")
"""

from __future__ import annotations

import asyncio
from typing import Literal

import flet as ft

from nano_offline.core import sound
from nano_offline.core.theme import Colors, Radius, Shadow

ToastKind = Literal["success", "error", "warning", "info"]

# (icon, accent token, icon-chip tint, gradient-end token)
_STYLE: dict[ToastKind, tuple[str, str, str, str]] = {
    "success": (ft.Icons.CHECK_ROUNDED, "SUCCESS", "SUCCESS_BG", "SUCCESS_DARKER"),
    "error": (ft.Icons.PRIORITY_HIGH_ROUNDED, "DANGER", "DANGER_BG", "DANGER_DARKER"),
    "warning": (ft.Icons.WARNING_ROUNDED, "WARNING", "WARNING_BG", "WARNING_DARKER"),
    "info": (ft.Icons.INFO_ROUNDED, "PRIMARY", "PRIMARY_BG", "PRIMARY_DARK"),
}

_EDGE_MARGIN = 18
_OFFSCREEN = 160.0
_ANIM = ft.Animation(240, ft.AnimationCurve.EASE_OUT)
_TICK = 0.06  # seconds between progress-bar updates

# Calibrated against the app's actual message strings (see every existing
# notify()/_notify() call site) rather than a generic guess -- success
# messages in this codebase consistently start with "تم"/"تمت"/"✔", hard
# failures name themselves ("خطأ", "تعذر", "فشل", "غير موجود"...), and
# blocking validation prompts read as "يجب"/"اختر"/"لا تملك"/etc.
_SUCCESS_MARKERS = ("تم ", "تمت ", "✔", "بنجاح")
_ERROR_MARKERS = ("خطأ", "تعذر", "فشل", "غير مهيأ", "غير موجود", "غير مطابق", "لا توجد", "لا يمكن", "رفض")
_WARNING_MARKERS = ("يجب", "اختر", "لا تملك", "أولاً", "أولًا", "الرجاء", "فارغ")


def _infer_kind(text: str) -> ToastKind:
    t = (text or "").strip()
    if t.startswith(_SUCCESS_MARKERS) or any(m in t for m in _SUCCESS_MARKERS):
        return "success"
    if any(m in t for m in _ERROR_MARKERS):
        return "error"
    if any(m in t for m in _WARNING_MARKERS):
        return "warning"
    return "info"


def _host_controls(host):
    return host if isinstance(host, list) else host.controls


def _host_update(host) -> None:
    if isinstance(host, list):
        return
    try:
        host.update()
    except Exception:
        pass


_MODAL_TYPES = tuple(
    t for t in (ft.BottomSheet, getattr(ft, "AlertDialog", None), getattr(ft, "CupertinoAlertDialog", None)) if t is not None
)


def register_modal(page: ft.Page, control) -> None:
    """Enable toasts *inside* an open sheet/dialog.

    Flet 0.28 pushes bottom sheets and dialogs as Navigator routes above the
    page's overlay, so a toast appended to ``page.overlay`` is hidden behind
    them. Wrap the modal's content in a ``Stack`` and remember it, so
    :func:`toast` can mount the card inside the top-most open modal instead.
    """
    if not isinstance(control, _MODAL_TYPES):
        return
    content = getattr(control, "content", None)
    if content is None:
        return
    if getattr(control, "_nano_toast_stack", None) is None:
        try:
            stack = ft.Stack([content], fit=ft.StackFit.LOOSE)
            stack.rtl = True
            expand = getattr(content, "expand", None)
            if expand:
                stack.expand = expand
            control.content = stack
            control._nano_toast_stack = stack
        except Exception:
            return
    stack_list = getattr(page, "_nano_modal_stack", None)
    if stack_list is None:
        stack_list = []
        page._nano_modal_stack = stack_list
    if control not in stack_list:
        stack_list.append(control)


def unregister_modal(page: ft.Page, control) -> None:
    stack_list = getattr(page, "_nano_modal_stack", None)
    if stack_list and control in stack_list:
        stack_list.remove(control)


def _active_modal(page: ft.Page):
    stack_list = getattr(page, "_nano_modal_stack", None)
    if stack_list:
        for control in reversed(stack_list):
            if getattr(control, "open", False):
                return control
    return None


def _active_toast_host(page: ft.Page):
    """The stack of the top-most *open* sheet/dialog, else the page overlay."""
    modal = _active_modal(page)
    if modal is not None:
        host = getattr(modal, "_nano_toast_stack", None)
        if host is not None:
            return host
    return page.overlay


def _prefer_top(page: ft.Page) -> bool:
    """Top placement inside modals (above the footer) and on full-screen
    surfaces like POS (so the card never covers the checkout bar)."""
    if _active_modal(page) is not None:
        return True
    return str(getattr(page, "_nano_toast_position", "") or "").lower() == "top"


def _dismiss(page: ft.Page, wrapper: ft.Container, host) -> None:
    if getattr(wrapper, "_nano_dismissed", False):
        return
    wrapper._nano_dismissed = True
    try:
        wrapper.opacity = 0
        wrapper.offset = ft.Offset(0, 0.15 if _prefer_top(page) else -0.15)
        wrapper.update()
    except Exception:
        pass

    async def _remove() -> None:
        await asyncio.sleep(_ANIM.duration / 1000)
        controls = _host_controls(host)
        if wrapper in controls:
            controls.remove(wrapper)
            _host_update(host)
            try:
                page.update()
            except Exception:
                pass
        if getattr(page, "_nano_toast", None) is wrapper:
            page._nano_toast = None

    try:
        page.run_task(_remove)
    except Exception:
        controls = _host_controls(host)
        if wrapper in controls:
            controls.remove(wrapper)


def toast(
    page: ft.Page,
    text: str,
    kind: ToastKind | None = None,
    duration: int = 2600,
    sound_kind: str | None = None,
    *,
    title: str | None = None,
    subtitle: str | None = None,
) -> None:
    """Show a floating toast card.

    ``kind`` controls the accent color/icon (success/error/warning/info);
    when omitted it's inferred from the message text. ``sound_kind`` lets a
    call site play a more specific tone than the four visual kinds cover.
    ``title``/``subtitle`` render a two-line card (falling back to ``text``
    when no title is given).
    """
    resolved_kind: ToastKind = kind or _infer_kind(text)
    icon_name, accent_token, _tint_token, deep_token = _STYLE[resolved_kind]
    accent = getattr(Colors, accent_token)
    deep = getattr(Colors, deep_token)

    sound.play(page, sound_kind or resolved_kind)

    primary_text = title if title is not None else text
    secondary_text = subtitle

    text_lines: list[ft.Control] = [
        ft.Text(
            primary_text, size=12.5, weight=ft.FontWeight.W_700,
            color=Colors.TEXT_PRIMARY, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )
    ]
    if secondary_text:
        text_lines.append(
            ft.Text(
                secondary_text, size=11, color=Colors.TEXT_SECONDARY,
                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
            )
        )

    progress = ft.ProgressBar(
        value=1.0, height=3, color=accent, bgcolor=Colors.BORDER, border_radius=2,
    )
    try:
        available = int(getattr(page, "width", 0) or 0)
    except Exception:
        available = 0
    card_width = 420 if available <= 0 else max(240, min(420, available - 48))

    pill = ft.Container(
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(width=4, height=34, border_radius=3, bgcolor=accent),
                        ft.Container(
                            ft.Icon(icon_name, color=Colors.WHITE, size=18),
                            width=32, height=32, alignment=ft.alignment.center,
                            border_radius=Radius.SM,
                            gradient=ft.LinearGradient(
                                colors=[deep, accent],
                                begin=ft.alignment.top_left,
                                end=ft.alignment.bottom_right,
                            ),
                        ),
                        ft.Column(text_lines, spacing=1, tight=True, expand=True),
                        ft.IconButton(
                            icon=ft.Icons.CLOSE_ROUNDED,
                            icon_size=14,
                            icon_color=Colors.TEXT_FAINT,
                            tooltip="إغلاق",
                            width=26, height=26,
                            on_click=lambda _=None: _dismiss(page, wrapper, host),
                        ),
                    ],
                    spacing=10,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                progress,
            ],
            spacing=8,
            tight=True,
        ),
        bgcolor=Colors.WHITE,
        border=ft.border.all(1, Colors.BORDER),
        border_radius=Radius.MD,
        padding=ft.padding.only(left=10, right=6, top=9, bottom=7),
        shadow=Shadow.LG,
        width=card_width,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        ink=True,
        on_click=lambda _=None: _dismiss(page, wrapper, host),
        animate_scale=ft.Animation(220, ft.AnimationCurve.EASE_OUT),
        scale=0.96,
        opacity=0,
    )

    host = _active_toast_host(page)
    top_position = _prefer_top(page)
    wrapper = ft.Container(
        content=ft.Row([pill], alignment=ft.MainAxisAlignment.CENTER),
        left=0,
        right=0,
        bottom=None if top_position else -_OFFSCREEN,
        top=-_OFFSCREEN if top_position else None,
        opacity=0,
        animate_position=_ANIM,
        animate_opacity=_ANIM,
    )

    paused = {"value": False}
    pill.on_hover = lambda e: paused.__setitem__("value", str(getattr(e, "data", "")).lower() == "true")

    # Replace any live toast (single slot) instead of stacking.
    previous = getattr(page, "_nano_toast", None)
    if previous is not None:
        prev_host = getattr(previous, "_nano_host", page.overlay)
        prev_controls = _host_controls(prev_host)
        if previous in prev_controls:
            prev_controls.remove(previous)
            _host_update(prev_host)

    controls = _host_controls(host)
    wrapper._nano_host = host
    wrapper._nano_dismissed = False
    page._nano_toast = wrapper
    controls.append(wrapper)
    _host_update(host)
    try:
        page.update()
    except Exception:
        pass

    # Entrance: slide in + scale up + fade in.
    async def _enter() -> None:
        await asyncio.sleep(0.01)
        if top_position:
            wrapper.top = _EDGE_MARGIN
        else:
            wrapper.bottom = _EDGE_MARGIN
        wrapper.opacity = 1
        pill.scale = 1.0
        pill.opacity = 1
        try:
            page.update()
        except Exception:
            pass

    try:
        page.run_task(_enter)
    except Exception:
        if top_position:
            wrapper.top = _EDGE_MARGIN
        else:
            wrapper.bottom = _EDGE_MARGIN
        wrapper.opacity = 1
        pill.opacity = 1

    total = max(0.4, float(duration) / 1000.0)

    async def _countdown() -> None:
        remaining = total
        while remaining > 0:
            await asyncio.sleep(_TICK)
            if getattr(page, "_nano_toast", None) is not wrapper:
                return
            if paused["value"]:
                continue  # pointer is over the card: hold the timer
            remaining -= _TICK
            try:
                progress.value = max(0.0, remaining / total)
                progress.update()
            except Exception:
                pass
        _dismiss(page, wrapper, host)

    try:
        page.run_task(_countdown)
    except Exception:
        pass


__all__ = ["toast", "ToastKind", "register_modal", "unregister_modal"]
