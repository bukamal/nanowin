"""Compact "add item" TABLE sheet, openable from anywhere.

POS and the invoice sheets used to send the user to the Materials screen (and
its step-by-step wizard) when a scanned barcode matched nothing. This sheet
opens over whatever screen is showing instead, pre-fills the scanned barcode,
and reports the new item back through ``on_saved`` so the caller can drop it
straight into the cart/invoice line.
"""

from __future__ import annotations

import flet as ft

from nano_offline.core.toast import toast
from nano_offline.core.theme import Colors, Shadow
from nano_offline.components import DataGrid, GridColumn, sheets
from nano_offline.components.buttons import primary_button


def open_new_item_sheet(page, ctx, *, barcode: str = "", notify=None, on_saved=None) -> None:
    def _notify(text, kind=None, sound_kind=None):
        if notify is not None:
            try:
                notify(text, kind=kind, sound_kind=sound_kind)
            except TypeError:
                try:
                    notify(text, kind=kind)
                except Exception:
                    pass
            except Exception:
                pass
            return
        try:
            toast(page, text, kind=kind)
        except Exception:
            pass

    categories = ctx.definitions.list_categories()
    units = ctx.definitions.list_units()
    default_unit = str(units[0]["id"]) if units else None
    cat_choices = [(str(c["id"]), c["name"]) for c in categories]
    unit_choices = [(str(u["id"]), u["name"]) for u in units]
    type_choices = [("مخزون", "مخزون"), ("خدمة", "خدمة")]

    prev_handler = getattr(page, "on_keyboard_event", None)
    sheet, sheet_w = sheets.new_desktop_sheet(page)

    def _picker_options(col, _row):
        if col.key == "category_id":
            return cat_choices
        if col.key == "base_unit_id":
            return unit_choices
        if col.key == "item_type":
            return type_choices
        return []

    grid = DataGrid(
        columns=[
            GridColumn("name", "اسم المادة *"),
            GridColumn("barcode", "الباركود", width=170),
            GridColumn("category_id", "التصنيف", width=150, editable=True, kind="picker"),
            GridColumn("base_unit_id", "الوحدة", width=130, editable=True, kind="picker"),
            GridColumn("item_type", "النوع", width=110, editable=True, kind="picker"),
            GridColumn("purchase_price", "سعر الشراء", width=130, editable=True, kind="number", align="end"),
            GridColumn("selling_price", "سعر البيع", width=130, editable=True, kind="number", align="end"),
            GridColumn("quantity", "الكمية", width=100, editable=True, kind="number", align="end"),
        ],
        rows=[{
            "name": "", "barcode": str(barcode or ""),
            "item_type": "مخزون", "base_unit_id": default_unit, "quantity": 1,
        }],
        page=page,
        center_cells=True,
        boxed=True,
        picker_options=_picker_options,
        empty_text="أدخل اسم المادة",
    )

    def _num(value, default=0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _close(_=None) -> None:
        try:
            grid.detach()
        except Exception:
            pass
        try:
            page.on_keyboard_event = prev_handler
        except Exception:
            pass
        page.close(sheet)

    def save(_=None) -> None:
        row = grid.row_at(0) or {}
        name = str(row.get("name") or "").strip()
        if not name:
            _notify("اسم المادة مطلوب", kind="error", sound_kind="barcode_error")
            return
        try:
            item_id = ctx.items.create(
                name=name,
                barcode=(str(row.get("barcode") or "").strip() or None),
                category_id=int(row["category_id"]) if row.get("category_id") else None,
                base_unit_id=int(row["base_unit_id"]) if row.get("base_unit_id") else None,
                item_type=str(row.get("item_type") or "مخزون"),
                purchase_price=_num(row.get("purchase_price")),
                selling_price=_num(row.get("selling_price")),
                quantity=_num(row.get("quantity")),
            )
        except Exception as exc:
            _notify(str(exc), kind="error")
            return
        _close()
        if on_saved is not None:
            try:
                on_saved(int(item_id))
            except Exception:
                pass
        _notify("تمت إضافة المادة", kind="success")

    def _escape_close() -> None:
        row = grid.row_at(0) or {}
        if not str(row.get("name") or "").strip():
            _close()
            return
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("حفظ المادة الجديدة؟"),
            content=ft.Text("أدخلت بيانات مادة ولم تحفظها بعد."),
            actions=[
                ft.TextButton("متابعة التحرير", on_click=lambda _: page.close(dialog)),
                ft.TextButton("خروج بدون حفظ", on_click=lambda _: (page.close(dialog), _close())),
                ft.FilledButton("حفظ", on_click=lambda _: (page.close(dialog), save())),
            ],
            actions_alignment=ft.MainAxisAlignment.CENTER,
        )
        page.open(dialog)

    sheet._nano_escape_close = _escape_close

    header = sheets.sheet_header(
        title="إضافة مادة جديدة",
        subtitle="تُضاف بالباركود الممسوح" if barcode else "املأ بيانات المادة ثم احفظ",
        icon=ft.Icons.ADD_BOX_OUTLINED,
        on_close=_close,
    )
    sheet.content = ft.Container(
        ft.Column(
            [
                ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER),
                header,
                ft.Container(grid, height=240),
                ft.Row([primary_button("حفظ", icon=ft.Icons.SAVE_OUTLINED, on_click=save)]),
            ],
            spacing=12, tight=True,
        ),
        padding=ft.padding.only(left=18, right=18, top=12, bottom=18),
        width=sheet_w, bgcolor=Colors.WHITE,
        border_radius=ft.border_radius.only(top_left=28, top_right=28),
        shadow=Shadow.LG,
    )
    sheet.content.width = sheet_w
    page.open(sheet)


__all__ = ["open_new_item_sheet"]
