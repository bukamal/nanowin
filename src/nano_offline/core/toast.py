"""A modern floating toast + progress card -- replacement for ``ft.SnackBar``.

Flet's built-in ``SnackBar`` sits flush against the bottom edge and is hard
to style consistently. :func:`toast` instead floats its own card in
``page.overlay`` (or inside the top-most open sheet/dialog -- see
``register_modal``). One reusable slot lives on the page object itself, so a
new toast replaces the previous one instead of stacking duplicates.

Visual language
---------------
* a colored accent bar + a gradient icon chip (kind aware),
* an optional bold title with a secondary detail line,
* a slim progress bar (auto-dismiss countdown, or real operation progress),
* an optional inline action button (e.g. «تراجع»),
* a spring-like slide/scale entrance and a quick slide-out,
* placement chosen by context: bottom-center normally, top-center inside an
  open sheet or on full-screen surfaces so it never covers footer actions.

Two entry points:

    toast(page, "تم الحفظ")                       # quick confirmation
    toast(page, "تم حذف المادة", action=("تراجع", undo_fn), duration=5200)
    h = toast_progress(page, "جارٍ إنشاء النسخة")   # sticky, no auto-dismiss
    h.update(0.42, subtitle="نسخ قاعدة البيانات")
    h.done("تم إنشاء النسخة", kind="success")
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Literal

import flet as ft

from nano_offline.core import sound
from nano_offline.core.theme import Colors, Radius, Shadow

ToastKind = Literal["success", "error", "warning", "info"]

# (icon, accent token, tint token, gradient-end token)
_STYLE: dict[ToastKind, tuple[str, str, str, str]] = {
    "success": (ft.Icons.CHECK_ROUNDED, "SUCCESS", "SUCCESS_BG", "SUCCESS_DARKER"),
    "error": (ft.Icons.PRIORITY_HIGH_ROUNDED, "DANGER", "DANGER_BG", "DANGER_DARKER"),
    "warning": (ft.Icons.WARNING_ROUNDED, "WARNING", "WARNING_BG", "WARNING_DARKER"),
    "info": (ft.Icons.INFO_ROUNDED, "PRIMARY", "PRIMARY_BG", "PRIMARY_DARK"),
}

_EDGE_MARGIN = 18
_OFFSCREEN = 160.0
_ANIM = ft.Animation(240, ft.AnimationCurve.EASE_OUT)
_TICK = 0.06  # seconds between countdown updates
_UNSET = object()

# (kept from the previous implementation) message -> kind inference.
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

    Flet pushes bottom sheets and dialogs as Navigator routes above the
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
    surfaces (via the ``_nano_toast_position`` hint)."""
    if _active_modal(page) is not None:
        return True
    return str(getattr(page, "_nano_toast_position", "") or "").lower() == "top"


class ToastHandle:
    """A live toast/progress card. Returned by :func:`toast`/:func:`toast_progress`."""

    def __init__(
        self,
        page: ft.Page,
        *,
        text: str,
        kind: ToastKind,
        mode: str = "toast",
        duration: int = 2600,
        sound_kind: str | None = None,
        title: str | None = None,
        subtitle: str | None = None,
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None:
        self.page = page
        self.mode = mode
        self.kind: ToastKind = kind
        self.action = action
        self._dismissed = False
        self._action_done = False
        self._paused = False
        self._countdown_cancelled = False
        self._last_update = 0.0
        self._lock = threading.Lock()

        primary = title if title is not None else text
        self.title_text = ft.Text(
            primary or "", size=12.5, weight=ft.FontWeight.W_700,
            color=Colors.TEXT_PRIMARY, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self.subtitle_text = ft.Text(
            subtitle or "", size=11, color=Colors.TEXT_SECONDARY,
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, visible=bool(subtitle),
        )
        self.accent_bar = ft.Container(width=4, height=34, border_radius=3, bgcolor=Colors.PRIMARY)
        self.icon = ft.Icon(ft.Icons.INFO_ROUNDED, color=Colors.WHITE, size=18)
        self.icon_chip = ft.Container(
            self.icon, width=32, height=32, alignment=ft.alignment.center,
            border_radius=Radius.SM,
        )
        self.bar = ft.ProgressBar(
            value=0.0 if mode == "progress" else 1.0,
            height=3, color=Colors.PRIMARY, bgcolor=Colors.BORDER, border_radius=2,
        )
        self.action_button = None
        if action is not None:
            self.action_button = ft.TextButton(
                str(action[0]),
                icon=ft.Icons.UNDO_ROUNDED,
                on_click=self._run_action,
            )

        available = 0
        try:
            available = int(getattr(page, "width", 0) or 0)
        except Exception:
            available = 0
        self.card_width = 420 if available <= 0 else max(240, min(440, available - 48))

        self.pill = self._build_pill()
        self._apply_kind(self.kind)
        self.top_position = _prefer_top(page)
        self.host = _active_toast_host(page)
        self.wrapper = self._build_wrapper()
        self._mount()

        if sound_kind or mode == "toast":
            sound.play(page, sound_kind or self.kind)
        try:
            page.run_task(self._run_enter)
        except Exception:
            self._place_visible()
        if mode == "toast" and duration and duration > 0:
            try:
                page.run_task(self._run_countdown, max(0.4, float(duration) / 1000.0))
            except Exception:
                pass

    # ------------------------------------------------------------- building
    def _build_pill(self) -> ft.Container:
        row_controls: list[ft.Control] = [
            self.accent_bar,
            self.icon_chip,
            ft.Column([self.title_text, self.subtitle_text], spacing=1, tight=True, expand=True),
        ]
        if self.action_button is not None:
            row_controls.append(self.action_button)
        row_controls.append(
            ft.IconButton(
                icon=ft.Icons.CLOSE_ROUNDED,
                icon_size=14,
                icon_color=Colors.TEXT_FAINT,
                tooltip="إغلاق",
                width=26, height=26,
                on_click=lambda _=None: self.dismiss(),
            )
        )
        pill = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        row_controls,
                        spacing=10, tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.bar,
                ],
                spacing=8, tight=True,
            ),
            bgcolor=Colors.WHITE,
            border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.MD,
            padding=ft.padding.only(left=10, right=6, top=9, bottom=7),
            shadow=Shadow.LG,
            width=self.card_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ink=True,
            on_click=lambda _=None: self.dismiss(),
            animate_scale=ft.Animation(220, ft.AnimationCurve.EASE_OUT),
            scale=0.96,
            opacity=0,
        )
        pill.on_hover = self._on_hover
        return pill

    def _build_wrapper(self) -> ft.Container:
        return ft.Container(
            content=ft.Row([self.pill], alignment=ft.MainAxisAlignment.CENTER),
            left=0, right=0,
            bottom=None if self.top_position else -_OFFSCREEN,
            top=-_OFFSCREEN if self.top_position else None,
            opacity=0,
            animate_position=_ANIM,
            animate_opacity=_ANIM,
        )

    def _apply_kind(self, kind: ToastKind) -> None:
        self.kind = kind
        icon_name, accent_token, _tint, deep_token = _STYLE[kind]
        accent = getattr(Colors, accent_token)
        deep = getattr(Colors, deep_token)
        self.accent_bar.bgcolor = accent
        self.icon.name = icon_name
        self.icon_chip.gradient = ft.LinearGradient(
            colors=[deep, accent], begin=ft.alignment.top_left, end=ft.alignment.bottom_right,
        )
        self.bar.color = accent
        if self.action_button is not None:
            try:
                self.action_button.style = ft.ButtonStyle(
                    color=accent, padding=ft.padding.symmetric(horizontal=8),
                )
            except Exception:
                pass

    def _on_hover(self, e) -> None:
        self._paused = str(getattr(e, "data", "")).lower() == "true"

    def _place_visible(self) -> None:
        if self.top_position:
            self.wrapper.top = _EDGE_MARGIN
        else:
            self.wrapper.bottom = _EDGE_MARGIN
        self.wrapper.opacity = 1
        self.pill.opacity = 1
        self.pill.scale = 1.0

    async def _run_enter(self) -> None:
        await asyncio.sleep(0.01)
        if self._dismissed:
            return
        self._place_visible()
        try:
            self.page.update()
        except Exception:
            pass

    def _mount(self) -> None:
        previous = getattr(self.page, "_nano_toast", None)
        if previous is not None:
            prev_host = getattr(previous, "_nano_host", self.page.overlay)
            prev_controls = _host_controls(prev_host)
            if previous in prev_controls:
                prev_controls.remove(previous)
                _host_update(prev_host)
        controls = _host_controls(self.host)
        self.wrapper._nano_host = self.host
        self.page._nano_toast = self.wrapper
        controls.append(self.wrapper)
        _host_update(self.host)
        try:
            self.page.update()
        except Exception:
            pass

    # --------------------------------------------------------- lifecycle ---
    async def _run_countdown(self, total: float) -> None:
        remaining = total
        while remaining > 0 and not self._dismissed:
            await asyncio.sleep(_TICK)
            if getattr(self.page, "_nano_toast", None) is not self.wrapper:
                return
            if self._countdown_cancelled:
                return
            if self._paused:
                continue
            remaining -= _TICK
            try:
                self.bar.value = max(0.0, remaining / total)
                self.bar.update()
            except Exception:
                pass
        if not self._dismissed:
            self.dismiss()

    def _schedule_dismiss(self, duration_ms: int) -> None:
        async def _later() -> None:
            await asyncio.sleep(max(0.1, duration_ms / 1000.0))
            self.dismiss()

        try:
            self.page.run_task(_later)
        except Exception:
            pass

    def dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self._countdown_cancelled = True
        try:
            self.wrapper.opacity = 0
            self.wrapper.offset = ft.Offset(0, 0.15 if self.top_position else -0.15)
            self.wrapper.update()
        except Exception:
            pass

        async def _remove() -> None:
            await asyncio.sleep(_ANIM.duration / 1000)
            controls = _host_controls(self.host)
            if self.wrapper in controls:
                controls.remove(self.wrapper)
                _host_update(self.host)
                try:
                    self.page.update()
                except Exception:
                    pass
            if getattr(self.page, "_nano_toast", None) is self.wrapper:
                self.page._nano_toast = None

        try:
            self.page.run_task(_remove)
        except Exception:
            controls = _host_controls(self.host)
            if self.wrapper in controls:
                controls.remove(self.wrapper)

    def _run_action(self, _=None) -> None:
        if self._action_done or self.action is None:
            return
        self._action_done = True
        callback = self.action[1]
        self.dismiss()
        try:
            callback()
        except Exception:
            pass

    # -------------------------------------------------------- public API ---
    def update(
        self,
        value=_UNSET,
        *,
        title=_UNSET,
        subtitle=_UNSET,
        kind=_UNSET,
    ) -> "ToastHandle":
        """Update a progress/managed toast in place."""
        if self._dismissed:
            return self
        if kind is not _UNSET and kind != self.kind:
            self._apply_kind(kind)  # type: ignore[arg-type]
        if title is not _UNSET:
            self.title_text.value = str(title or "")
        if subtitle is not _UNSET:
            self.subtitle_text.value = str(subtitle or "")
            self.subtitle_text.visible = bool(subtitle)
        if value is not _UNSET:
            if value is None:
                self.bar.value = None
            else:
                try:
                    self.bar.value = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    pass
        now = time.monotonic()
        if now - self._last_update < 0.05 and (value is _UNSET or value not in (1.0, None)):
            return self
        self._last_update = now
        try:
            self.page.update()
        except Exception:
            pass
        return self

    def done(self, text: str | None = None, *, kind: ToastKind = "success", duration: int = 1800) -> None:
        """Finish a progress toast: full bar, success/error style, auto-dismiss."""
        if text is not None:
            self.title_text.value = text
        self.subtitle_text.value = ""
        self.subtitle_text.visible = False
        self._apply_kind(kind)
        try:
            self.bar.value = 1.0
        except Exception:
            pass
        try:
            sound.play(self.page, kind)
        except Exception:
            pass
        self._countdown_cancelled = True
        try:
            self.page.update()
        except Exception:
            pass
        self._schedule_dismiss(duration)


def toast(
    page: ft.Page,
    text: str,
    kind: ToastKind | None = None,
    duration: int = 2600,
    sound_kind: str | None = None,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    action: tuple[str, Callable[[], None]] | None = None,
) -> ToastHandle:
    """Show a floating toast card (see the module docstring)."""
    resolved_kind: ToastKind = kind or _infer_kind(text or title or subtitle or "")
    return ToastHandle(
        page, text=text, kind=resolved_kind, mode="toast",
        duration=duration, sound_kind=sound_kind, title=title, subtitle=subtitle,
        action=action,
    )


def toast_progress(
    page: ft.Page,
    text: str,
    *,
    kind: ToastKind = "info",
    title: str | None = None,
    subtitle: str | None = None,
    sound_kind: str | None = None,
) -> ToastHandle:
    """Show a sticky progress card; drive it with ``handle.update()``/``done()``."""
    return ToastHandle(
        page, text=text, kind=kind, mode="progress",
        duration=0, sound_kind=sound_kind, title=title, subtitle=subtitle,
    )


__all__ = ["toast", "toast_progress", "ToastHandle", "ToastKind", "register_modal", "unregister_modal"]
