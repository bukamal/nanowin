"""Compact "add item" TABLE panel, usable standalone or inside a sheet.

Two entry points:
* :func:`build_new_item_panel` returns an ``ft.Control`` card (a small editable
  table). Invoice sheets embed this in their own in-sheet *Stack* overlay, so
  opening it over a BottomSheet never dismisses that sheet (a dialog/route over
  a bottom sheet does).
* :func:`open_new_item_sheet` wraps the same panel in a standalone BottomSheet
  for callers that are not already inside one (e.g. POS).

Both report the new item id through ``on_saved``.
"""

from __future__ import annotations

import flet as ft

from nano_offline.core.theme import Colors, Shadow
from nano_offline.components import DataGrid, GridColumn, sheets
from nano_offline.components.buttons import primary_button


def _notifier(notify):
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
    return _notify


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_new_item_panel(page, ctx, *, barcode: str = "", name: str = "", notify=None,
                         on_saved=None, on_close=None) -> ft.Control:
    """Build the add-item table card (no wrapper). Callers own dismissal via
    ``on_close`` and receive the created item id via ``on_saved``."""
    _notify = _notifier(notify)
    categories = ctx.definitions.list_categories()
    units = ctx.definitions.list_units()
    default_unit = str(units[0]["id"]) if units else None
    cat_choices = [(str(c["id"]), c["name"]) for c in categories]
    unit_choices = [(str(u["id"]), u["name"]) for u in units]
    type_choices = [("مخزون", "مخزون"), ("خدمة", "خدمة")]

    prev_handler = getattr(page, "on_keyboard_event", None)

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
            "name": str(name or ""), "barcode": str(barcode or ""),
            "item_type": "مخزون", "base_unit_id": default_unit, "quantity": 1,
        }],
        page=page,
        center_cells=True,
        boxed=True,
        picker_options=_picker_options,
        empty_text="أدخل اسم المادة",
    )

    def _finish() -> None:
        try:
            grid.detach()
        except Exception:
            pass
        try:
            page.on_keyboard_event = prev_handler
        except Exception:
            pass
        if on_close is not None:
            try:
                on_close()
            except Exception:
                pass

    def save(_=None) -> None:
        row = grid.row_at(0) or {}
        item_name = str(row.get("name") or "").strip()
        if not item_name:
            _notify("اسم المادة مطلوب", kind="error", sound_kind="barcode_error")
            return
        try:
            item_id = ctx.items.create(
                name=item_name,
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
        if on_saved is not None:
            try:
                on_saved(int(item_id))
            except Exception:
                pass
        _finish()
        _notify("تمت إضافة المادة", kind="success")

    header = ft.Row(
        [
            ft.Icon(ft.Icons.ADD_BOX_OUTLINED, size=20, color=Colors.PRIMARY),
            ft.Text("إضافة مادة جديدة", size=16, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY, expand=True),
            ft.IconButton(icon=ft.Icons.CLOSE, tooltip="إغلاق", on_click=lambda _=None: _finish()),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    card = ft.Container(
        ft.Column(
            [
                header,
                ft.Text("أدخل البيانات ثم احفظ — تُضاف فورًا للفاتورة/السلة.", size=11, color=Colors.TEXT_SECONDARY),
                ft.Container(grid, expand=True),
                ft.Row(
                    [
                        ft.TextButton("إلغاء", on_click=lambda _=None: _finish()),
                        primary_button("حفظ", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
                    ],
                    alignment=ft.MainAxisAlignment.END,
                ),
            ],
            spacing=12, expand=True,
        ),
        width=max(360, min(980, int(getattr(page, "width", 1000) or 1000) - 60)),
        height=max(320, min(640, int((getattr(page, "height", 760) or 760) * 0.82))),
        padding=16, bgcolor=Colors.WHITE,
        border_radius=18, shadow=Shadow.LG, rtl=True,
    )
    card.data = {"save": save, "cancel": _finish}
    return card


def open_new_item_sheet(page, ctx, *, barcode: str = "", name: str = "", notify=None, on_saved=None) -> None:
    """Standalone BottomSheet wrapper around :func:`build_new_item_panel`."""
    sheet, sheet_w = sheets.new_desktop_sheet(page)

    def _close_sheet() -> None:
        try:
            page.close(sheet)
        except Exception:
            pass

    panel = build_new_item_panel(
        page, ctx, barcode=barcode, name=name,
        notify=notify, on_saved=on_saved, on_close=_close_sheet,
    )
    sheet._nano_escape_close = lambda: panel.data["cancel"]()
    sheet.content = ft.Container(
        ft.Column(
            [ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER), panel],
            spacing=8, tight=True,
        ),
        width=sheet_w, bgcolor=Colors.WHITE,
        border_radius=ft.border_radius.only(top_left=28, top_right=28),
        padding=ft.padding.only(top=10, bottom=12),
        shadow=Shadow.LG,
    )
    sheet.content.width = sheet_w
    page.open(sheet)


__all__ = ["open_new_item_sheet", "build_new_item_panel"]
