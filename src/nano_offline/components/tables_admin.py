"""Admin sheet to manage restaurant tables (name + area)."""

from __future__ import annotations

import flet as ft

from nano_offline.components import SelectAllTextField, sheets
from nano_offline.components.buttons import primary_button, secondary_button, inline_icon_button
from nano_offline.core.theme import Colors, Radius

__all__ = ["open_tables_admin"]


def open_tables_admin(page: ft.Page, ctx, notify) -> None:
    sheet, width = sheets.new_desktop_sheet(page, cap=720)
    list_col = ft.Column(spacing=8, tight=True)

    def close(_=None) -> None:
        page.close(sheet)

    def render() -> None:
        tables = ctx.tables.list()
        controls: list[ft.Control] = []
        if not tables:
            controls.append(ft.Text(
                "لا توجد طاولات بعد. أضف طاولاتك (مع المنطقة) لتظهر كتسميات جاهزة في نقطة البيع.",
                size=12, color=Colors.TEXT_SECONDARY,
            ))
        current_area = None
        for table in tables:
            area = str(table.get("area") or "").strip()
            if area != current_area:
                current_area = area
                controls.append(ft.Text(area or "بلا منطقة", size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK))
            controls.append(_row(table))
        list_col.controls = controls
        try:
            list_col.update()
        except Exception:
            pass

    def _row(table: dict) -> ft.Control:
        return ft.Container(
            ft.Row(
                [
                    ft.Icon(
                        ft.Icons.APPS_ROUNDED,
                        size=18,
                        color=Colors.PRIMARY if int(table.get("active") or 0) else Colors.TEXT_FAINT,
                    ),
                    ft.Text(str(table.get("name") or ""), size=13, weight=ft.FontWeight.W_600, expand=True),
                    inline_icon_button(ft.Icons.EDIT_OUTLINED, lambda _, t=table: open_form(t), tooltip="تعديل"),
                    inline_icon_button(ft.Icons.DELETE_OUTLINE, lambda _, t=table: confirm_delete(t), tooltip="حذف", color=Colors.DANGER),
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.all(10),
            border=ft.border.all(1, Colors.BORDER_ALT),
            border_radius=Radius.MD,
        )

    def confirm_delete(table: dict) -> None:
        dialog = ft.AlertDialog(modal=True, title=ft.Text("حذف طاولة"))

        def yes(_=None) -> None:
            try:
                ctx.tables.delete(int(table["id"]))
            except Exception as exc:
                notify(str(exc), kind="error")
            page.close(dialog)
            render()

        dialog.content = ft.Text(f"سيُحذف «{table.get('name')}». هل تريد المتابعة؟")
        dialog.actions = [ft.TextButton("إلغاء", on_click=lambda _=None: page.close(dialog)), ft.FilledButton("حذف", on_click=yes)]
        page.open(dialog)

    def open_form(table: dict | None = None) -> None:
        editing = table is not None
        dialog = ft.AlertDialog(modal=True)
        name_f = SelectAllTextField(label="اسم/رقم الطاولة *", value=(table or {}).get("name") or "")
        area_f = SelectAllTextField(label="المنطقة (صالة/تراس/خارجي)", value=(table or {}).get("area") or "")
        order_f = SelectAllTextField(label="الترتيب", value=str((table or {}).get("sort_order") or 0), keyboard_type=ft.KeyboardType.NUMBER)
        active_f = ft.Switch(value=bool((table or {}).get("active", True)) if table else True, label="مفعّلة")
        status = ft.Text("", size=11, color=Colors.DANGER)

        def save(_=None) -> None:
            try:
                payload = dict(
                    name=name_f.value or "",
                    area=area_f.value or "",
                    sort_order=int(order_f.value or 0),
                    active=bool(active_f.value),
                )
                if editing:
                    ctx.tables.update(int(table["id"]), **payload)
                else:
                    ctx.tables.create(**payload)
            except Exception as exc:
                status.value = str(exc)
                status.update()
                return
            page.close(dialog)
            render()
            notify("تم حفظ الطاولة", kind="success")

        dialog.title = ft.Text("تعديل طاولة" if editing else "طاولة جديدة")
        dialog.content = ft.Column([name_f, area_f, order_f, active_f, status], spacing=9, tight=True, width=400)
        dialog.actions = [
            ft.TextButton("إلغاء", on_click=lambda _=None: page.close(dialog)),
            ft.FilledButton("حفظ", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
        ]
        page.open(dialog)

    add_btn = primary_button("طاولة جديدة", icon=ft.Icons.ADD_ROUNDED, on_click=lambda _=None: open_form(), width=None, size="md")

    def open_bulk() -> None:
        dialog = ft.AlertDialog(modal=True)
        area_f = SelectAllTextField(label="المنطقة (اختياري)")
        start_f = SelectAllTextField(label="تبدأ من الرقم", value="1", keyboard_type=ft.KeyboardType.NUMBER)
        count_f = SelectAllTextField(label="العدد", value="10", keyboard_type=ft.KeyboardType.NUMBER)
        prefix_f = SelectAllTextField(label="بادئة الاسم (اختياري)")
        status = ft.Text("", size=11, color=Colors.DANGER)

        def save(_=None) -> None:
            try:
                created = ctx.tables.create_many(
                    area=area_f.value or "",
                    start=int(start_f.value or 1),
                    count=int(count_f.value or 0),
                    prefix=prefix_f.value or "",
                )
            except Exception as exc:
                status.value = str(exc)
                status.update()
                return
            page.close(dialog)
            render()
            notify(f"تمت إضافة {created} طاولة", kind="success")

        dialog.title = ft.Text("إنشاء طاولات بالعدد")
        dialog.content = ft.Column(
            [
                ft.Text("مثال: العدد 10 ينتج الطاولات 1..10 في المنطقة المحددة.", size=11, color=Colors.TEXT_SECONDARY),
                area_f,
                ft.Row(
                    [ft.Container(start_f, expand=True), ft.Container(count_f, expand=True)],
                    spacing=8,
                ),
                prefix_f,
                status,
            ],
            spacing=9, tight=True, width=400,
        )
        dialog.actions = [
            ft.TextButton("إلغاء", on_click=lambda _=None: page.close(dialog)),
            ft.FilledButton("إنشاء", icon=ft.Icons.ADD, on_click=save),
        ]
        page.open(dialog)

    bulk_btn = secondary_button("إنشاء بالعدد", icon=ft.Icons.ADD_CIRCLE_OUTLINE, on_click=lambda _=None: open_bulk(), width=None, size="md")
    sheet.content = sheets.sheet_frame(
        ft.Column(
            [
                ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER),
                sheets.sheet_header(
                    title="الطاولات",
                    subtitle="تسميات الطاولات والمناطق — تظهر في نقطة البيع ولوحة الطاولات",
                    icon=ft.Icons.APPS_ROUNDED,
                    on_close=close,
                    trailing=ft.Row([bulk_btn, add_btn], spacing=8),
                ),
                ft.Container(
                    ft.Column([list_col], scroll=ft.ScrollMode.AUTO),
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