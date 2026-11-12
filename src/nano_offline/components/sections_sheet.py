"""Sections & printers management sheet (shared by Admin).

Moved out of the items screen so the whole restaurant/print setup lives in one
place (Admin → Restaurant). Renders: restaurant-mode toggle, per-section
report, print-queue status with retry, open orders, and full section CRUD
(network printer, width, copies, encoding, fallback, per-device override,
preview and test print).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import flet as ft

from nano_offline.components import SearchSelect, SegmentedToggle, SegmentOption, SelectAllTextField, sheets
from nano_offline.components.buttons import primary_button, secondary_button, inline_icon_button
from nano_offline.components.print_profile import preview_document
from nano_offline.core import restaurant_settings
from nano_offline.core.theme import Colors, Radius

__all__ = ["open_sections_sheet"]


def _confirm(page: ft.Page, *, title: str, message: str, on_confirm) -> None:
    dialog = ft.AlertDialog(modal=True, title=ft.Text(title))

    def yes(_=None) -> None:
        page.close(dialog)
        on_confirm()

    def no(_=None) -> None:
        page.close(dialog)

    dialog.content = ft.Text(message)
    dialog.actions = [ft.TextButton("إلغاء", on_click=no), ft.FilledButton("متابعة", on_click=yes)]
    page.open(dialog)


def open_sections_sheet(page: ft.Page, ctx, notify, money, native_files=None) -> None:
    """Open the sections/printers management sheet.

    ``notify(text, kind=..., sound_kind=...)`` and ``money(value)`` are injected
    so the same sheet works from the Admin view and any other host.
    """
    sheet, width = sheets.new_desktop_sheet(page, cap=860)
    list_col = ft.Column(spacing=8, tight=True)
    report_col = ft.Column(spacing=4, tight=True)
    queue_col = ft.Column(spacing=4, tight=True)
    period_state = {"key": "all"}

    def _period_range() -> tuple[str | None, str | None]:
        today = date.today()
        key = period_state["key"]
        if key == "today":
            return today.isoformat(), today.isoformat()
        if key == "week":
            return (today - timedelta(days=6)).isoformat(), today.isoformat()
        if key == "month":
            return today.replace(day=1).isoformat(), today.isoformat()
        return None, None

    def _render_queue() -> None:
        counts = ctx.section_print.counts()
        controls: list[ft.Control] = [
            ft.Text("حالة طابور الطباعة", size=12, weight=ft.FontWeight.W_600),
            ft.Row(
                [
                    ft.Text(f"بالانتظار: {counts.get('pending', 0)}", size=11, color=Colors.WARNING_DARK),
                    ft.Text(f"نجحت: {counts.get('sent', 0)}", size=11, color=Colors.SUCCESS_DARK),
                    ft.Text(f"فشلت: {counts.get('failed', 0)}", size=11, color=Colors.DANGER),
                ],
                spacing=12, wrap=True,
            ),
        ]
        if counts.get("failed", 0):
            def _retry(_=None) -> None:
                try:
                    n = ctx.section_print.retry_failed()
                    ctx.section_print.dispatch_async()
                except Exception as exc:
                    notify(str(exc), kind="error")
                    return
                notify(f"أُعيد جدولة {n} مهمة فاشلة")
                _render_queue()
            controls.append(secondary_button("إعادة إرسال الفاشلة", icon=ft.Icons.REFRESH, on_click=_retry, width=None, size="md"))
        queue_col.controls = controls
        try:
            queue_col.update()
        except Exception:
            pass

    def close(_=None) -> None:
        page.close(sheet)

    def _restaurant_mode_control() -> ft.Control:
        open_count = len(ctx.invoices.open_sale_orders())

        def _toggle(e) -> None:
            restaurant_settings.set_enabled(ctx.settings, bool(e.control.value))
            notify("تم تحديث وضع المطاعم")

        return ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.RESTAURANT_OUTLINED, size=17, color=Colors.PRIMARY),
                            ft.Text("وضع المطاعم (الطلب قبل الدفع)", size=12, weight=ft.FontWeight.W_600, expand=True),
                            ft.Switch(value=restaurant_settings.enabled(ctx.settings), on_change=_toggle),
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "عند التفعيل يمكن حفظ فاتورة بيع غير مدفوعة دون اختيار عميل (تُنسب إلى «طلبات مفتوحة»)، "
                        f"وتُطبع تذاكر الأقسام فورًا. طلبات مفتوحة حاليًا: {open_count}",
                        size=10, color=Colors.TEXT_SECONDARY,
                    ),
                ],
                spacing=4, tight=True,
            ),
            padding=ft.padding.all(10),
            bgcolor=Colors.PRIMARY_BG,
            border_radius=Radius.MD,
        )

    _PERIOD_LABELS = {"today": "اليوم", "week": "آخر 7 أيام", "month": "هذا الشهر", "all": "كل الفترات"}
    period_dd = SegmentedToggle(
        options=[
            SegmentOption("today", "اليوم"),
            SegmentOption("week", "7 أيام"),
            SegmentOption("month", "هذا الشهر"),
            SegmentOption("all", "الكل"),
        ],
        value="all",
    )

    def _on_period(_=None) -> None:
        period_state["key"] = period_dd.value or "all"
        render_report()

    period_dd.on_change = _on_period

    def render_report() -> None:
        date_from, date_to = _period_range()
        rows = ctx.sections.sales_by_section(date_from=date_from, date_to=date_to)
        controls: list[ft.Control] = [
            ft.Text(
                f"مبيعات الأقسام ({_PERIOD_LABELS.get(period_state['key'], 'كل الفترات')})",
                size=12, weight=ft.FontWeight.W_600,
            ),
        ]
        if not rows:
            controls.append(ft.Text("لا توجد مبيعات مرحّلة بعد.", size=11, color=Colors.TEXT_SECONDARY))
        else:
            controls.append(ft.Row(
                [
                    ft.Text("القسم", size=10, color=Colors.TEXT_SECONDARY, expand=True),
                    ft.Text("المبيعات", size=10, color=Colors.TEXT_SECONDARY, width=90, text_align=ft.TextAlign.END),
                    ft.Text("التكلفة", size=10, color=Colors.TEXT_SECONDARY, width=90, text_align=ft.TextAlign.END),
                    ft.Text("الربح", size=10, color=Colors.TEXT_SECONDARY, width=90, text_align=ft.TextAlign.END),
                ],
                spacing=6,
            ))
            for row in rows:
                profit = float(row.get("profit") or 0)
                controls.append(ft.Row(
                    [
                        ft.Text(str(row.get("section_name") or "بلا قسم"), size=11, expand=True),
                        ft.Text(money(float(row.get("revenue") or 0)), size=11, width=90, text_align=ft.TextAlign.END),
                        ft.Text(money(float(row.get("cost") or 0)), size=11, width=90, text_align=ft.TextAlign.END),
                        ft.Text(money(profit), size=11, width=90, text_align=ft.TextAlign.END,
                                color=Colors.SUCCESS_DARK if profit >= 0 else Colors.DANGER),
                    ],
                    spacing=6,
                ))
        controls.append(ft.Divider(height=1))
        controls.append(ft.Text("الطلبات المفتوحة (بانتظار السداد)", size=12, weight=ft.FontWeight.W_600))
        orders = ctx.invoices.open_sale_orders(limit=20)
        if not orders:
            controls.append(ft.Text("لا توجد طلبات مفتوحة.", size=11, color=Colors.TEXT_SECONDARY))
        else:
            for order in orders:
                label = str(order.get("reference") or "").strip() or f"طلب #{order['id']}"
                controls.append(ft.Row(
                    [
                        ft.Text(label, size=11, expand=True),
                        ft.Text(
                            money(float(order.get("remaining_amount") or 0)),
                            size=11, width=100, text_align=ft.TextAlign.END, color=Colors.DANGER,
                        ),
                    ],
                    spacing=6,
                ))
        report_col.controls = controls
        try:
            report_col.update()
        except Exception:
            pass

    def render() -> None:
        sections = ctx.sections.list()
        controls: list[ft.Control] = []
        if not sections:
            controls.append(ft.Text(
                "لا توجد أقسام بعد. أضف قسمًا (مطبخ/بار/...) وحدّد طابعته الشبكية، "
                "ثم أسند المواد إليه من عمود «القسم» في جدول المواد.",
                size=12, color=Colors.TEXT_SECONDARY,
            ))
        for section in sections:
            controls.append(_section_card(section))
        list_col.controls = controls
        try:
            list_col.update()
        except Exception:
            pass
        render_report()
        _render_queue()

    def toggle_active(section_id: int, section: dict, value: bool) -> None:
        try:
            ctx.sections.update(
                section_id,
                name=str(section.get("name") or ""),
                kind=str(section.get("kind") or "kitchen"),
                printer_host=section.get("printer_host"),
                printer_port=int(section.get("printer_port") or 9100),
                paper_width_mm=int(section.get("paper_width_mm") or 80),
                copies=int(section.get("copies") or 1),
                encoding=str(section.get("encoding") or "cp1256"),
                active=bool(value),
                sort_order=int(section.get("sort_order") or 0),
                fallback_section_id=int(section["fallback_section_id"]) if section.get("fallback_section_id") else None,
            )
            render()
        except Exception as exc:
            notify(str(exc), kind="error")

    def test_print(section: dict) -> None:
        async def _run() -> None:
            try:
                await asyncio.to_thread(ctx.section_print.test_print, int(section["id"]))
                notify("تم إرسال الطباعة التجريبية", kind="success")
            except Exception as exc:
                notify(f"تعذرت الطباعة: {exc}", kind="error")
        page.run_task(_run)

    def preview_ticket(section: dict) -> None:
        async def _run() -> None:
            if native_files is None:
                notify("المعاينة غير مهيأة في هذا البناء", kind="error")
                return
            html = ctx.documents.section_ticket_preview_html(
                section, [{"name": "مادة تجريبية", "quantity": 1}]
            )
            try:
                result = await preview_document(
                    native_files,
                    html,
                    name="nano-section-ticket",
                    layout="roll",
                    roll_width_mm=int(section.get("paper_width_mm") or 80),
                )
                notify("تم فتح المعاينة في المتصفح." if result == "browser" else "تم تجهيز معاينة PDF.")
            except Exception as exc:
                notify(f"تعذرت المعاينة: {exc}", kind="error")
        page.run_task(_run)

    def _section_card(section: dict) -> ft.Control:
        host = str(section.get("printer_host") or "").strip()
        target = f"{host}:{section.get('printer_port') or 9100}" if host else "بلا طابعة"
        kind_label = "مطبخ" if str(section.get("kind") or "kitchen") != "receipt" else "فاتورة"
        override = ctx.section_print.device_override(int(section["id"]))
        subtitle = f"{kind_label} • {target} • {section.get('paper_width_mm')}مم ×{section.get('copies')}"
        if override:
            subtitle += f" • هذا الجهاز: {override}"
        return ft.Container(
            ft.Row(
                [
                    ft.Icon(
                        ft.Icons.PRINT_OUTLINED,
                        color=Colors.PRIMARY if int(section.get("active") or 0) else Colors.TEXT_FAINT,
                        size=18,
                    ),
                    ft.Column(
                        [
                            ft.Text(str(section.get("name") or ""), size=13, weight=ft.FontWeight.W_600),
                            ft.Text(subtitle, size=10, color=Colors.TEXT_SECONDARY),
                        ],
                        spacing=0, expand=True, tight=True,
                    ),
                    ft.Switch(
                        value=bool(int(section.get("active") or 0)),
                        on_change=lambda e, sid=int(section["id"]), sec=section: toggle_active(sid, sec, e.control.value),
                    ),
                    inline_icon_button(ft.Icons.SETTINGS_ETHERNET, lambda _, sec=section: open_override(sec), tooltip="تجاوز طابعة هذا الجهاز"),
                    inline_icon_button(ft.Icons.VISIBILITY_OUTLINED, lambda _, sec=section: preview_ticket(sec), tooltip="معاينة التذكرة"),
                    inline_icon_button(ft.Icons.EDIT_OUTLINED, lambda _, sec=section: open_form(sec), tooltip="تعديل"),
                    inline_icon_button(ft.Icons.CONTACT_PHONE_OUTLINED, lambda _, sec=section: test_print(sec), tooltip="طباعة تجريبية", color=Colors.SUCCESS),
                    inline_icon_button(ft.Icons.DELETE_OUTLINE, lambda _, sec=section: confirm_delete(sec), tooltip="حذف", color=Colors.DANGER),
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.all(10),
            border=ft.border.all(1, Colors.BORDER_ALT),
            border_radius=Radius.MD,
        )

    def open_override(section: dict) -> None:
        sid = int(section["id"])
        dialog = ft.AlertDialog(modal=True)
        field = SelectAllTextField(
            label="عنوان طابعة هذا الجهاز (IP أو IP:منفذ)",
            value=ctx.section_print.device_override(sid),
        )
        status = ft.Text("", size=11, color=Colors.DANGER)

        def save(_=None) -> None:
            try:
                ctx.section_print.set_device_override(sid, field.value or "")
            except Exception as exc:
                status.value = str(exc)
                status.update()
                return
            page.close(dialog)
            render()
            notify("تم تحديث تجاوز طابعة هذا الجهاز", sound_kind="success")

        dialog.title = ft.Text("تجاوز طابعة هذا الجهاز")
        dialog.content = ft.Column(
            [
                ft.Text(
                    "اتركه فارغًا لاستخدام طابعة القسم المشتركة. هذا التجاوز يُطبَّق على هذا الجهاز فقط.",
                    size=11, color=Colors.TEXT_SECONDARY,
                ),
                field,
                status,
            ],
            spacing=8, tight=True, width=420,
        )
        dialog.actions = [
            ft.TextButton("إلغاء", on_click=lambda _=None: page.close(dialog)),
            ft.FilledButton("حفظ", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
        ]
        page.open(dialog)

    def confirm_delete(section: dict) -> None:
        _confirm(
            page,
            title="حذف قسم",
            message=f"سيُحذف القسم «{section.get('name')}» وتُلغى إسنادات المواد إليه. هل تريد المتابعة؟",
            on_confirm=lambda: (ctx.sections.delete(int(section["id"])), render()),
        )

    def open_form(section: dict | None = None) -> None:
        editing = section is not None
        dialog = ft.AlertDialog(modal=True)
        name_f = SelectAllTextField(label="اسم القسم *", value=(section or {}).get("name") or "")
        host_f = SelectAllTextField(label="عنوان الطابعة (IP)", value=(section or {}).get("printer_host") or "")
        port_f = SelectAllTextField(label="المنفذ", value=str((section or {}).get("printer_port") or 9100), keyboard_type=ft.KeyboardType.NUMBER)
        copies_f = SelectAllTextField(label="عدد النسخ", value=str((section or {}).get("copies") or 1), keyboard_type=ft.KeyboardType.NUMBER)
        kind_f = SearchSelect(
            label="النوع",
            choices=[("kitchen", "مطبخ / تحضير"), ("receipt", "فاتورة")],
            value=str((section or {}).get("kind") or "kitchen"),
            allow_clear=False,
        )
        width_f = SearchSelect(
            label="عرض الورق",
            choices=[("80", "80 مم"), ("58", "58 مم")],
            value=str((section or {}).get("paper_width_mm") or 80),
            allow_clear=False,
        )
        encoding_f = SearchSelect(
            label="ترميز العربية",
            choices=[("cp1256", "Windows Arabic (cp1256)"), ("cp864", "Arabic DOS (cp864)"), ("cp850", "cp850")],
            value=str((section or {}).get("encoding") or "cp1256"),
            allow_clear=False,
        )
        fallback_f = SearchSelect(
            label="طابعة بديلة عند الفشل (اختياري)",
            choices=[
                (str(s["id"]), s["name"])
                for s in ctx.sections.list()
                if not section or int(s["id"]) != int(section["id"])
            ],
            value=str(section.get("fallback_section_id")) if section and section.get("fallback_section_id") else None,
        )
        active_f = ft.Switch(value=bool((section or {}).get("active", True)) if section else True, label="مفعّل")
        status = ft.Text("", size=11, color=Colors.DANGER)

        def save(_=None) -> None:
            try:
                payload = dict(
                    name=name_f.value or "",
                    kind=kind_f.value or "kitchen",
                    printer_host=(host_f.value or "").strip() or None,
                    printer_port=int(port_f.value or 9100),
                    paper_width_mm=int(width_f.value or 80),
                    copies=int(copies_f.value or 1),
                    encoding=encoding_f.value or "cp1256",
                    active=bool(active_f.value),
                    sort_order=int((section or {}).get("sort_order") or 0),
                    fallback_section_id=int(fallback_f.value) if fallback_f.value else None,
                )
                if editing:
                    ctx.sections.update(int(section["id"]), **payload)
                else:
                    ctx.sections.create(**payload)
            except Exception as exc:
                status.value = str(exc)
                status.update()
                return
            page.close(dialog)
            render()
            notify("تم حفظ القسم", kind="success")

        dialog.title = ft.Text("تعديل قسم" if editing else "قسم جديد")
        dialog.content = ft.Column(
            [
                name_f,
                ft.ResponsiveRow([
                    ft.Container(host_f, col={"xs": 12, "md": 8}),
                    ft.Container(port_f, col={"xs": 6, "md": 4}),
                ], spacing=7, run_spacing=7),
                ft.ResponsiveRow([
                    ft.Container(kind_f, col={"xs": 6}),
                    ft.Container(width_f, col={"xs": 6}),
                ], spacing=7, run_spacing=7),
                ft.ResponsiveRow([
                    ft.Container(copies_f, col={"xs": 6}),
                    ft.Container(encoding_f, col={"xs": 6}),
                ], spacing=7, run_spacing=7),
                fallback_f,
                active_f,
                status,
            ],
            spacing=9, tight=True, width=440,
        )
        dialog.actions = [
            ft.TextButton("إلغاء", on_click=lambda _=None: page.close(dialog)),
            ft.FilledButton("حفظ", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
        ]
        page.open(dialog)

    add_btn = primary_button("قسم جديد", icon=ft.Icons.ADD_ROUNDED, on_click=lambda _=None: open_form(), width=None, size="md")
    sheet.content = sheets.sheet_frame(
        ft.Column(
            [
                ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER),
                sheets.sheet_header(
                    title="الأقسام والطابعات",
                    subtitle="محطات الطباعة الشبكية — طابعة واحدة لكل قسم",
                    icon=ft.Icons.PRINT_OUTLINED,
                    on_close=close,
                    trailing=add_btn,
                ),
                ft.Container(
                    ft.Column(
                        [
                            _restaurant_mode_control(),
                            ft.Text(
                                "أسند كل مادة إلى قسمها من عمود «القسم» في بطاقة المادة أو شيت الجدول، "
                                "وعند البيع تُطبع تذكرة تحضير على طابعة القسم.",
                                size=10, color=Colors.TEXT_SECONDARY,
                            ),
                            ft.Container(
                                ft.Column(
                                    [
                                        ft.Row([ft.Icon(ft.Icons.BAR_CHART_OUTLINED, size=16, color=Colors.PRIMARY),
                                                ft.Text("تقرير الأقسام", size=12, weight=ft.FontWeight.W_600)], spacing=6),
                                        period_dd,
                                        report_col,
                                    ],
                                    spacing=6, tight=True,
                                ),
                                padding=ft.padding.all(10),
                                bgcolor=Colors.BACKGROUND_ALT,
                                border_radius=Radius.MD,
                            ),
                            ft.Container(
                                ft.Column([queue_col], spacing=6, tight=True),
                                padding=ft.padding.all(10),
                                bgcolor=Colors.WARNING_BG,
                                border_radius=Radius.MD,
                            ),
                            ft.Divider(height=1),
                            list_col,
                        ],
                        spacing=8, tight=True, scroll=ft.ScrollMode.AUTO,
                    ),
                    expand=True,
                ),
            ],
            spacing=10, expand=True,
        ),
        width=width,
        height=sheets.sheet_height(page, 0.8),
    )
    render()
    page.open(sheet)