from __future__ import annotations

import flet as ft

from nano_offline.core import branding
from nano_offline.core.toast import toast

from nano_offline.components import PatternPad, SelectAllTextField, kpi_card, metric_controls
from nano_offline.core.theme import Colors, Radius, Shadow


class SecurityCenter:
    """Quick-auth (PIN/pattern) setup and saved-login status for the current user.

    Extracted from ``main.py`` (previously the inline ``show_security`` closure).
    """

    def __init__(self, page: ft.Page, ctx, content: ft.Container, *, on_title_change=None):
        self.page = page
        self.ctx = ctx
        self.content = content
        self.on_title_change = on_title_change

    def _set_header(self, title: str, subtitle: str = "") -> None:
        if self.on_title_change:
            self.on_title_change(title, subtitle)

    def notify(self, text: str, kind: str | None = None, sound_kind: str | None = None) -> None:
        toast(self.page, text, kind=kind, sound_kind=sound_kind)

    # -- shared bottom-sheet shell -----------------------------------------
    # The app's established modal bottom sheet: drag handle, rounded top
    # corners, white surface, LG shadow. Centralized here so pin/pattern/clear
    # all look identical.

    @staticmethod
    def _icon_bubble(icon: str, *, color: str = Colors.PRIMARY, bgcolor: str = Colors.PRIMARY_BG) -> ft.Container:
        return ft.Container(
            ft.Icon(icon, color=color, size=24),
            width=48,
            height=48,
            alignment=ft.alignment.center,
            bgcolor=bgcolor,
            border_radius=Radius.MD,
        )

    def _open_sheet(self, *, icon: str, icon_color: str, icon_bg: str, title: str, subtitle: str, body: ft.Control, actions: list[ft.Control]) -> ft.BottomSheet:
        sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)
        sheet.content = ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10)],
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            self._icon_bubble(icon, color=icon_color, bgcolor=icon_bg),
                            ft.Column(
                                [
                                    ft.Text(title, size=18, weight=ft.FontWeight.BOLD),
                                    ft.Text(subtitle, size=11, color=Colors.TEXT_SECONDARY),
                                ],
                                spacing=1,
                                expand=True,
                            ),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Divider(height=1, color=Colors.BORDER_ALT),
                    body,
                    ft.Row(actions, spacing=10, alignment=ft.MainAxisAlignment.END),
                ],
                spacing=16,
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=ft.padding.only(left=20, right=20, top=12, bottom=26),
            bgcolor=Colors.WHITE,
            border_radius=ft.border_radius.only(top_left=28, top_right=28),
            shadow=Shadow.LG,
        )
        self.page.open(sheet)
        return sheet

    @staticmethod
    def _styled_field(**kwargs) -> ft.TextField:
        defaults = dict(
            border_radius=Radius.MD,
            filled=True,
            bgcolor=Colors.BACKGROUND_ALT,
            border_color=Colors.BORDER,
            focused_border_color=Colors.PRIMARY,
        )
        defaults.update(kwargs)
        return SelectAllTextField(**defaults)

    def show_center(self) -> None:
        page = self.page
        ctx = self.ctx
        content = self.content
        notify = self.notify
        self._set_header("الأمان والدخول", "إدارة PIN والنمط والجلسة المحفوظة على هذا الجهاز")
        session = ctx.auth.current()
        if session is None:
            raise RuntimeError("لا توجد جلسة مستخدم")

        # Same KPI-strip language as every other screen (items, finance,
        # statements): the current quick-auth kind and the auto-login state
        # are the two facts this screen exists to communicate, so they are
        # live status cards instead of plain grey sentences.
        summary = ft.ResponsiveRow(spacing=8, run_spacing=8)

        def refresh_security():
            kind = ctx.auth.quick_auth_info(session.username)
            quick_value, quick_icon, quick_accent = {
                "pin": ("PIN", ft.Icons.PIN_ROUNDED, Colors.PRIMARY),
                "pattern": ("نمط", ft.Icons.APPS_ROUNDED, Colors.PURPLE),
            }.get(kind, ("غير مفعّل", ft.Icons.LOCK_OUTLINE_ROUNDED, Colors.TEXT_MUTED))
            saved_on = ctx.auth.saved_login_enabled(session.username)
            summary.controls = metric_controls([
                kpi_card("الدخول السريع", quick_value, quick_icon, quick_accent),
                kpi_card(
                    "الدخول التلقائي على هذا الجهاز",
                    "مفعّل" if saved_on else "غير مفعّل",
                    ft.Icons.CHECK_CIRCLE_OUTLINE if saved_on else ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED,
                    Colors.SUCCESS if saved_on else Colors.TEXT_MUTED,
                ),
            ])
            page.update()

        def pin_dialog(_=None):
            current_password = self._styled_field(label="كلمة المرور الحالية", password=True, can_reveal_password=True, prefix_icon=ft.Icons.LOCK_OUTLINE_ROUNDED)
            pin = self._styled_field(
                label="PIN جديد (4–8 أرقام)",
                password=True,
                can_reveal_password=True,
                keyboard_type=ft.KeyboardType.NUMBER,
                prefix_icon=ft.Icons.PIN_ROUNDED,
            )
            confirm = self._styled_field(
                label="تأكيد PIN",
                password=True,
                can_reveal_password=True,
                keyboard_type=ft.KeyboardType.NUMBER,
                prefix_icon=ft.Icons.PIN_ROUNDED,
            )

            def close(_=None):
                page.close(sheet)

            def save(_=None):
                try:
                    if (pin.value or "") != (confirm.value or ""):
                        raise ValueError("تأكيد PIN غير مطابق")
                    ctx.auth.set_quick_auth("pin", pin.value or "", current_password.value or "")
                    page.close(sheet)
                    notify("تم تفعيل الدخول بـ PIN")
                    refresh_security()
                except Exception as exc:
                    notify(str(exc), kind="error")

            sheet = self._open_sheet(
                icon=ft.Icons.LOCK_ROUNDED,
                icon_color=Colors.PRIMARY,
                icon_bg=Colors.PRIMARY_BG,
                title="إعداد الدخول بـ PIN",
                subtitle=f"رقم سرّي قصير لفتح {branding.APP_NAME_AR} بسرعة على هذا الجهاز",
                body=ft.Column([current_password, pin, confirm], spacing=10, tight=True),
                actions=[
                    ft.TextButton("إلغاء", on_click=close),
                    ft.FilledButton("حفظ PIN", icon=ft.Icons.CHECK_ROUNDED, on_click=save),
                ],
            )

        def pattern_dialog(_=None):
            # Two-step wizard (draw, then re-draw to confirm) instead of showing
            # both grids at once: a single 252x252 canvas fits without the
            # sheet needing to scroll while drawing, and a scrollable ancestor
            # around a drag-based canvas is exactly what stops the pattern
            # from being drawable at all -- Flutter's gesture arena can hand a
            # vertical drag to the nearest scrollable ancestor instead of the
            # pad. This also mirrors how Android/iOS actually ask for pattern
            # confirmation: one grid at a time, not two stacked together.
            current_password = self._styled_field(label="كلمة المرور الحالية", password=True, can_reveal_password=True, prefix_icon=ft.Icons.LOCK_OUTLINE_ROUNDED)
            state = {"first_value": None}
            body = ft.Column(spacing=14, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

            def close(_=None):
                page.close(sheet)

            def show_step1():
                state["first_value"] = None

                def captured(value: str):
                    state["first_value"] = value
                    show_step2()

                pad = PatternPad(on_complete=captured)
                body.controls = [
                    current_password,
                    ft.Text("ارسم نمطًا جديدًا (4 نقاط على الأقل)", weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER),
                    pad,
                ]
                page.update()

            def show_step2():
                def confirmed(value: str):
                    try:
                        if not (current_password.value or "").strip():
                            raise ValueError("أدخل كلمة المرور الحالية")
                        if value != state["first_value"]:
                            notify("النمط غير مطابق -- ارسم من جديد")
                            show_step1()
                            return
                        ctx.auth.set_quick_auth("pattern", value, current_password.value or "")
                        page.close(sheet)
                        notify("تم تفعيل الدخول بالنمط")
                        refresh_security()
                    except Exception as exc:
                        notify(str(exc), kind="error")

                pad = PatternPad(on_complete=confirmed)
                body.controls = [
                    current_password,
                    ft.Text("أعد رسم نفس النمط للتأكيد", weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER),
                    pad,
                    ft.TextButton("رسم نمط جديد بدل هذا", icon=ft.Icons.REFRESH, on_click=lambda _: show_step1()),
                ]
                page.update()

            sheet = self._open_sheet(
                icon=ft.Icons.APPS_ROUNDED,
                icon_color=Colors.PURPLE,
                icon_bg=Colors.PURPLE_BG,
                title="إعداد نمط الدخول",
                subtitle=f"ارسم نمطًا يربط 4 نقاط على الأقل لفتح {branding.APP_NAME_AR} بسرعة",
                body=body,
                actions=[ft.TextButton("إلغاء", on_click=close)],
            )
            show_step1()

        def clear_dialog(_=None):
            current_password = self._styled_field(label="كلمة المرور الحالية", password=True, can_reveal_password=True, prefix_icon=ft.Icons.LOCK_OUTLINE_ROUNDED)

            def close(_=None):
                page.close(sheet)

            def clear(_=None):
                try:
                    ctx.auth.clear_quick_auth(current_password.value or "")
                    page.close(sheet)
                    notify("تم إلغاء PIN / النمط")
                    refresh_security()
                except Exception as exc:
                    notify(str(exc), kind="error")

            sheet = self._open_sheet(
                icon=ft.Icons.REFRESH_ROUNDED,
                icon_color=Colors.DANGER,
                icon_bg=Colors.DANGER_BG,
                title="إلغاء الدخول السريع",
                subtitle="سيعود تسجيل الدخول إلى كلمة المرور الكاملة فقط",
                body=ft.Column([current_password], spacing=10, tight=True),
                actions=[
                    ft.TextButton("إلغاء", on_click=close),
                    ft.FilledButton(
                        "إلغاء الدخول السريع",
                        icon=ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED,
                        on_click=clear,
                    ),
                ],
            )

        initial = (session.full_name or session.username or "?").strip()[:1].upper() or "?"

        def action_tile(*, icon, color, bg, title, subtitle, on_click):
            return ft.Container(
                ft.Row(
                    [
                        self._icon_bubble(icon, color=color, bgcolor=bg),
                        ft.Column(
                            [
                                ft.Text(title, size=13, weight=ft.FontWeight.BOLD),
                                ft.Text(subtitle, size=10, color=Colors.TEXT_FAINT),
                            ],
                            spacing=1,
                            expand=True,
                        ),
                        ft.Icon(ft.Icons.CHEVRON_LEFT_ROUNDED, size=16, color=Colors.TEXT_FAINT),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=12,
                bgcolor=Colors.WHITE,
                border=ft.border.all(1, Colors.BORDER),
                border_radius=Radius.LG,
                shadow=Shadow.SM,
                ink=True,
                on_click=on_click,
            )

        section_header = ft.Row(
            [
                ft.Icon(ft.Icons.SHIELD_OUTLINED, size=15, color=Colors.TEXT_SECONDARY),
                ft.Text("خيارات الحماية", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY, expand=True),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=6,
        )

        hero_card = ft.Container(
            ft.Row(
                [
                    ft.Container(
                        ft.Text(initial, size=18, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                        width=48,
                        height=48,
                        alignment=ft.alignment.center,
                        bgcolor=Colors.PRIMARY,
                        border_radius=15,
                    ),
                    ft.Column(
                        [
                            ft.Text(session.full_name, size=16, weight=ft.FontWeight.BOLD),
                            ft.Text(f"{session.username} · حساب محلي على هذا الجهاز", size=11, color=Colors.TEXT_SECONDARY),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    ft.Container(
                        ft.Icon(ft.Icons.SHIELD_OUTLINED, color=Colors.PRIMARY, size=20),
                        width=40,
                        height=40,
                        alignment=ft.alignment.center,
                        bgcolor=Colors.PRIMARY_BG,
                        border_radius=13,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=14,
            bgcolor=Colors.WHITE,
            border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.LG,
            shadow=Shadow.SM,
        )

        note_card = ft.Container(
            ft.Row(
                [
                    ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=16, color=Colors.TEXT_SECONDARY),
                    ft.Text(
                        "لا يتم حفظ كلمة المرور كنص. خيار البقاء مسجلاً يستخدم رمز جلسة عشوائيًا محليًا، بينما PIN والنمط يحفظان كبصمة مشفرة مرتبطة بهذا الجهاز.",
                        size=11,
                        color=Colors.TEXT_SECONDARY,
                        expand=True,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            padding=12,
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.MD,
        )

        content.content = ft.Column(
            [
                hero_card,
                summary,
                section_header,
                action_tile(
                    icon=ft.Icons.PIN_ROUNDED,
                    color=Colors.PRIMARY,
                    bg=Colors.PRIMARY_BG,
                    title="إعداد الدخول بـ PIN",
                    subtitle=f"رقم سري قصير (4–8 أرقام) لفتح {branding.APP_NAME_AR} بسرعة على هذا الجهاز",
                    on_click=pin_dialog,
                ),
                action_tile(
                    icon=ft.Icons.APPS_ROUNDED,
                    color=Colors.PURPLE,
                    bg=Colors.PURPLE_BG,
                    title="إعداد نمط الدخول",
                    subtitle="ارسم نمطًا يربط 4 نقاط على الأقل",
                    on_click=pattern_dialog,
                ),
                action_tile(
                    icon=ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED,
                    color=Colors.DANGER,
                    bg=Colors.DANGER_BG,
                    title="إلغاء PIN / النمط",
                    subtitle="سيعود تسجيل الدخول إلى كلمة المرور الكاملة فقط",
                    on_click=clear_dialog,
                ),
                note_card,
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
        )
        refresh_security()
        page.update()


__all__ = ["SecurityCenter"]
