from __future__ import annotations

import asyncio
import csv as _csv
import tempfile
from datetime import datetime
from pathlib import Path

import flet as ft

from nano_offline.core.toast import toast

from nano_offline.components import SearchSelect, SegmentedToggle, SegmentOption, SelectAllTextField, SmartAmountField, empty_state, kpi_card, DataGrid, GridColumn, metric_controls, metric_row
from nano_offline.components.buttons import header_close_button, inline_icon_button
from nano_offline.components.command_bar import RibbonAction
from nano_offline.components.money_text import money_row
from nano_offline.core.theme import Colors, IconSize, Radius, Shadow
from nano_offline.core import currency
from nano_offline.core import barcode_quality
from nano_offline.core import barcode_settings
from nano_offline.core import item_card_signing
from nano_offline.core.platform_capabilities import supports

# Desktop grid columns. Only name + the two prices are inline-editable:
# quantity is derived from inventory movements (see ItemRepository.update,
# which has no quantity parameter) and the rest are display-only.
ITEM_GRID_COLUMNS = [
    GridColumn("name", "الاسم", editable=True, tooltip="اسم المادة أو الخدمة — قابل للتعديل مباشرة"),
    GridColumn("category_name", "التصنيف", width=120, tooltip="التصنيف المسجّل للمادة"),
    GridColumn("unit_name", "الوحدة", width=80, tooltip="الوحدة الأساسية للمادة"),
    GridColumn("item_type", "النوع", width=80, align="center", kind="badge", tooltip="مخزون (يُتابع) أو خدمة (لا مخزون)"),
    GridColumn("quantity", "الكمية", width=90, align="end", kind="number", tooltip="الكمية المتاحة حاليًا من حركات المخزون"),
    GridColumn("purchase_price", "سعر الشراء", width=110, align="end", kind="money", editable=True, tooltip="آخر سعر شراء — قابل للتعديل"),
    GridColumn("selling_price", "سعر البيع", width=110, align="end", kind="money", editable=True, tooltip="سعر البيع الحالي — قابل للتعديل"),
    GridColumn("_status", "الحالة", width=100, align="center", kind="badge", sortable=False, tooltip="متوفر / منخفض / نفد / خدمة"),
]


def _edit_item_cell_text(col: GridColumn, value, row: dict, settings) -> str | None:
    """Start a price edit in the display currency, not the stored USD."""
    if col.kind == "money":
        if value in (None, ""):
            return ""
        return currency.to_input_text(value, settings)
    return None


def _parse_item_cell(col: GridColumn, text: str, settings):
    """Convert a cell edit back to storage; money is stored as USD."""
    if col.kind == "money":
        return currency.parse_display_input(text, settings)
    cleaned = (text or "").strip()
    if col.kind == "number":
        cleaned = cleaned.replace(",", "")
        return float(cleaned) if cleaned else 0.0
    return cleaned


def _confirm_similar(page: ft.Page, *, title: str, message: str, on_confirm) -> None:
    """Non-blocking guard dialog: ask once, then let the caller proceed
    either way -- never silently reject, since two genuinely different
    records can legitimately look very close to each other (size variants
    of the same product, two branches with a near-identical name, etc).
    Shared by the barcode-similarity and name-similarity checks below.
    """
    dialog = ft.AlertDialog(modal=True, title=ft.Text(title))

    def yes(_=None) -> None:
        page.close(dialog)
        on_confirm()

    def no(_=None) -> None:
        page.close(dialog)

    dialog.content = ft.Text(message)
    dialog.actions = [ft.TextButton("إلغاء", on_click=no), ft.FilledButton("متابعة على أي حال", on_click=yes)]
    page.open(dialog)


def _confirm_similar_barcode(page: ft.Page, similar_names: list[str], on_confirm) -> None:
    """Barcode is a likely typo/scan-glitch away from another item's code
    (see ``core.barcode_quality.find_similar``)."""
    names = "، ".join(similar_names[:3])
    _confirm_similar(
        page,
        title="تنبيه تشابه باركود",
        message=(
            f"هذا الباركود قريب جدًا من باركود مادة أخرى ({names}) — قد يكون خطأ كتابة أو مسح. "
            "هل تريد المتابعة على أي حال؟"
        ),
        on_confirm=on_confirm,
    )


def _confirm_similar_name(page: ft.Page, similar_names: list[str], on_confirm) -> None:
    """Name is an exact or near-duplicate of another item already on file
    (see ``ItemRepository.find_similar_names``)."""
    names = "، ".join(similar_names[:3])
    _confirm_similar(
        page,
        title="تنبيه تشابه اسم",
        message=(
            f"يوجد بالفعل مادة باسم مشابه جدًا ({names}) — قد تكون هذه نفس المادة. "
            "هل تريد المتابعة وإضافتها كمادة منفصلة؟"
        ),
        on_confirm=on_confirm,
    )


def _icon_bubble(icon: str, *, color: str = Colors.PRIMARY, bgcolor: str = Colors.PRIMARY_BG) -> ft.Container:
    return ft.Container(
        ft.Icon(icon, color=color, size=24),
        width=48, height=48, alignment=ft.alignment.center,
        bgcolor=bgcolor, border_radius=Radius.MD,
    )


class _PagedSheetNav:
    """Passed into each page's builder so it can drive its own transitions
    (advance automatically after a successful scan, jump back to rescan,
    close outright) without the pages needing to know about each other."""

    def __init__(self, page: ft.Page, sheet: ft.BottomSheet, state: dict, renderer):
        self._page = page
        self._sheet = sheet
        self._state = state
        self._renderer = renderer

    def close(self) -> None:
        self._page.close(self._sheet)

    def goto(self, index: int) -> None:
        self._state["idx"] = max(0, min(self._state["count"] - 1, index))
        self._renderer()

    def next(self) -> None:
        self.goto(self._state["idx"] + 1)

    def back(self) -> None:
        self.goto(self._state["idx"] - 1)


def _open_paged_sheet(
    page: ft.Page,
    *,
    icon: str,
    icon_color: str = Colors.PRIMARY,
    icon_bg: str = Colors.PRIMARY_BG,
    title: str,
    page_count: int,
    render_page,
) -> ft.BottomSheet:
    """Multi-step bottom sheet: one focused screen per step, with a small
    dot indicator, instead of every field for a multi-part flow (issue a
    card / scan a card / confirm an import) stacked into a single tall,
    cluttered form. ``render_page(index, nav)`` must return
    ``(subtitle, body_control, footer_controls)`` for that step; ``nav``
    is a :class:`_PagedSheetNav` the page uses to advance, go back, or
    close. Reuses the same drag-handle / rounded-top / shadow shell as
    every other sheet in this app (security_view._open_sheet, form_sheet.py)."""
    sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)
    state = {"idx": 0, "count": page_count, "opened": False}

    subtitle_text = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
    body_holder = ft.Column([], spacing=14, tight=True)
    footer_holder = ft.Row([], spacing=10, alignment=ft.MainAxisAlignment.END)
    dots = ft.Row([], spacing=6, alignment=ft.MainAxisAlignment.CENTER)

    def render() -> None:
        subtitle, body, footer = render_page(state["idx"], nav)
        subtitle_text.value = subtitle
        body_holder.controls = [body]
        footer_holder.controls = footer
        dots.controls = [
            ft.Container(width=7, height=7, border_radius=4, bgcolor=Colors.PRIMARY if i == state["idx"] else Colors.BORDER_STRONG)
            for i in range(page_count)
        ]
        if state["opened"]:
            page.update()

    nav = _PagedSheetNav(page, sheet, state, render)

    render()  # initial content, before the sheet is attached -- no .update() yet
    sheet.content = ft.Container(
        ft.Column(
            [
                ft.Row([ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10)], alignment=ft.MainAxisAlignment.CENTER),
                ft.Row(
                    [
                        _icon_bubble(icon, color=icon_color, bgcolor=icon_bg),
                        ft.Column([ft.Text(title, size=18, weight=ft.FontWeight.BOLD), subtitle_text], spacing=1, expand=True),
                    ],
                    spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                *([dots] if page_count > 1 else []),
                ft.Divider(height=1, color=Colors.BORDER_ALT),
                body_holder,
                footer_holder,
            ],
            spacing=14, tight=True, scroll=ft.ScrollMode.AUTO,
        ),
        padding=ft.padding.only(left=20, right=20, top=12, bottom=26),
        bgcolor=Colors.WHITE,
        border_radius=ft.border_radius.only(top_left=28, top_right=28),
        shadow=Shadow.LG,
    )
    page.open(sheet)
    state["opened"] = True
    return sheet


class ItemsCenter:
    """Items/services list: search, filters, definitions dialog, add/edit, detail view.

    Extracted from ``main.py`` (previously the inline ``items_view`` closure).
    """

    def __init__(self, page: ft.Page, ctx, content: ft.Container, *, native_files=None, on_title_change=None, on_open_stocktake=None, on_context_actions=None):
        self.page = page
        self.ctx = ctx
        self.content = content
        self.native_files = native_files
        self.on_title_change = on_title_change
        self.on_open_stocktake = on_open_stocktake
        self.on_context_actions = on_context_actions

    def _set_header(self, title: str, subtitle: str = "") -> None:
        if self.on_title_change:
            self.on_title_change(title, subtitle)

    def money(self, value) -> str:
        return currency.format_amount(value, self.ctx.settings)

    @staticmethod
    def _qty(value) -> str:
        """Plain (non-currency) number formatting -- quantities, counts.

        No thousands separators and no forced decimals, so quantities never
        render with commas.
        """
        v = float(value or 0)
        return str(int(v)) if v == int(v) else f"{v:.3f}".rstrip("0").rstrip(".")

    def notify(self, text: str, kind: str | None = None, sound_kind: str | None = None) -> None:
        toast(self.page, text, kind=kind, sound_kind=sound_kind)

    def show_center(self, prefill_barcode: str | None = None) -> None:
        page = self.page
        ctx = self.ctx
        content = self.content
        notify = self.notify
        money = self.money
        qty_fmt = self._qty
        self._set_header("المواد", "إدارة المخزون والخدمات والتصنيفات والوحدات")
        categories = ctx.definitions.list_categories()
        units = ctx.definitions.list_units()
        # Late-bound: `grid` is created a few lines below; the lambda only
        # runs once the user focuses search, by which point it exists.
        search = SelectAllTextField(label="بحث في المواد", hint_text="اسم المادة أو الخدمة", prefix_icon=ft.Icons.SEARCH, on_focus=lambda e: grid.disarm())
        summary_row = ft.ResponsiveRow(spacing=8, run_spacing=8)
        filter_state = {"mode": "all"}
        # Render only a page of rows at a time instead of building a widget
        # for every matching item -- with a few thousand items, building
        # them all on every keystroke/filter change is what actually made
        # the screen feel slow (separate from the query itself). "تحميل
        # المزيد" grows this on demand instead of paying for everything
        # up front. Reset to the first page whenever the search/filter
        # changes, so results don't stay capped on the un-refined match.
        render_limit = {"n": 60}
        filter_boxes: dict[str, ft.Container] = {}
        sort_state = {"key": "name"}
        LOW_STOCK = 5.0

        # ---- desktop grid (pilot) --------------------------------------
        def _status_tuple(row: dict):
            if row.get("item_type") != "مخزون":
                return ("خدمة", Colors.PURPLE, Colors.PURPLE_BG)
            q = float(row.get("quantity") or 0)
            if q <= 0:
                return ("نفد", Colors.DANGER_DARK, Colors.DANGER_BG)
            if q < LOW_STOCK:
                return ("منخفض", Colors.WARNING_DARK, Colors.WARNING_BG_ALT)
            return ("متوفر", Colors.SUCCESS, Colors.SUCCESS_BG)

        def _grid_formatter(col, value, row):
            if col.key == "_status":
                return _status_tuple(row)
            if col.key == "item_type":
                return ("مخزون", Colors.PRIMARY, Colors.PRIMARY_BG) if row.get("item_type") == "مخزون" else ("خدمة", Colors.PURPLE, Colors.PURPLE_BG)
            if col.key == "category_name":
                return str(value or "بلا تصنيف")
            if col.key == "unit_name":
                return str(value or "بلا وحدة")
            if col.key == "quantity":
                return qty_fmt(value) if row.get("item_type") == "مخزون" else "—"
            if col.kind == "money":
                return money(value)
            return None  # text columns: default rendering

        def _grid_commit(row_index: int, key: str, value) -> None:
            # update() rewrites every field, so read the current row first and
            # change only the edited key -- otherwise a price edit would blank
            # the category/unit/barcode.
            row = grid.row_at(row_index) or {}
            item_id = int(row.get("id") or 0)
            try:
                fresh = ctx.items.get(item_id)
                if fresh is None:
                    raise ValueError("المادة غير موجودة")
                units = [
                    {"unit_id": u["id"], "conversion_factor": u.get("conversion_factor", 1),
                     "selling_price": u.get("selling_price"), "purchase_price": u.get("purchase_price")}
                    for u in ctx.items.units(item_id)
                ]
                kwargs = dict(
                    name=fresh["name"], category_id=fresh.get("category_id"),
                    item_type=fresh.get("item_type") or "مخزون",
                    purchase_price=fresh.get("purchase_price") or 0,
                    selling_price=fresh.get("selling_price") or 0,
                    base_unit_id=fresh.get("base_unit_id"), barcode=fresh.get("barcode"),
                    item_units=units,
                )
                kwargs[key] = value
                ctx.items.update(item_id, **kwargs)
            except Exception as exc:
                notify(str(exc), kind="error")
                raise  # let the grid revert the optimistic cell value

        grid_sort = {"key": None, "dir": None}
        density_state = {"compact": ctx.settings.get("ui.items_density", "comfortable") == "compact"}
        selected_state = {"rows": []}
        export_state = {"rows": []}
        hidden_state = {"keys": set(filter(None, ctx.settings.get("ui.items_hidden_columns", "").split(",")))}
        auto_hidden = {"keys": {"category_name", "unit_name"} if (page.width or 1200) < 720 else set()}

        def current_hidden() -> set[str]:
            return set(hidden_state["keys"]) | set(auto_hidden["keys"])

        def toggle_column(key: str) -> None:
            if key in hidden_state["keys"]:
                hidden_state["keys"].discard(key)
            else:
                hidden_state["keys"].add(key)
            ctx.settings.set("ui.items_hidden_columns", ",".join(sorted(hidden_state["keys"])))
            grid.set_hidden_keys(current_hidden())
            page.update()

        def column_menu_items() -> list:
            return [
                ft.PopupMenuItem(
                    text=c.label,
                    checked=c.key not in hidden_state["keys"],
                    on_click=lambda _, k=c.key: toggle_column(k),
                )
                for c in ITEM_GRID_COLUMNS
            ]

        columns_button = ft.PopupMenuButton(
            icon=ft.Icons.VIEW_COLUMN,
            tooltip="إظهار/إخفاء الأعمدة",
            items=column_menu_items(),
        )

        async def export_csv(_=None) -> None:
            rows = export_state["rows"]
            if not rows:
                notify("لا توجد بيانات للتصدير", kind="warning")
                return
            if self.native_files is None:
                notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                path = Path(tempfile.gettempdir()) / f"nano_items_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = _csv.writer(fh)
                    writer.writerow(["الاسم", "التصنيف", "الوحدة", "النوع", "الكمية", "سعر الشراء", "سعر البيع", "الباركود"])
                    for item in rows:
                        writer.writerow([
                            item.get("name") or "",
                            item.get("category_name") or "",
                            item.get("unit_name") or "",
                            item.get("item_type") or "",
                            item.get("quantity") or 0,
                            item.get("purchase_price") or 0,
                            item.get("selling_price") or 0,
                            item.get("barcode") or "",
                        ])
                await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير المواد")
                notify("تم تجهيز ملف CSV (بترميز يفتح في Excel العربي)")
            except Exception as exc:
                notify(str(exc), kind="error")

        export_button = ft.IconButton(
            icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
            tooltip="تصدير CSV للعرض الحالي",
            on_click=export_csv,
        )

        def item_row_bg(row: dict) -> str | None:
            if row.get("item_type") != "مخزون":
                return None
            qty = float(row.get("quantity") or 0)
            if qty <= 0:
                return Colors.DANGER_BG
            if qty < LOW_STOCK:
                return Colors.WARNING_BG_ALT
            return None

        def item_expand_panel(row: dict) -> ft.Control:
            item_id = int(row.get("id") or 0)
            stock_value = float(row.get("quantity") or 0) * float(row.get("average_cost") or 0)
            last_move = "لا حركات مخزون"
            try:
                moves = ctx.items.movements(item_id, limit=1)
                if moves:
                    mv = moves[0]
                    kind_label = mv.get("movement_type") or mv.get("kind") or "حركة"
                    last_move = f"{kind_label} • {mv.get('quantity')} • {str(mv.get('movement_date') or '')[:10]}"
            except Exception:
                pass

            def chip(icon, label: str, value: str):
                return ft.Container(
                    ft.Row(
                        [
                            ft.Icon(icon, size=15, color=Colors.PRIMARY),
                            ft.Text(f"{label}: ", size=11, color=Colors.TEXT_SECONDARY),
                            ft.Text(value, size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_PRIMARY),
                        ],
                        spacing=5,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=7),
                    border=ft.border.all(1, Colors.BORDER),
                    border_radius=10,
                    bgcolor=Colors.WHITE,
                )

            return ft.Row(
                [
                    chip(ft.Icons.QR_CODE_2_OUTLINED, "الباركود", str(row.get("barcode") or "بلا باركود")),
                    chip(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, "قيمة المخزون", money(stock_value)),
                    chip(ft.Icons.HISTORY_ROUNDED, "آخر حركة", last_move),
                ],
                spacing=8, wrap=True,
            )

        def focus_search() -> None:
            grid.disarm()
            try:
                search.focus()
            except Exception:
                pass

        def apply_grid_sort(key: str | None, direction: str | None) -> None:
            grid_sort["key"], grid_sort["dir"] = key, direction
            render_limit["n"] = 60
            refresh()

        def render_bulk_bar(rows: list[dict]) -> None:
            selected_state["rows"] = list(rows)
            if not rows:
                bulk_bar.visible = False
                bulk_bar.content = None
            else:
                bulk_bar.content = ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECK_BOX, size=16, color=Colors.PRIMARY),
                        ft.Text(f"تم تحديد {len(rows)}", size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK),
                        ft.OutlinedButton("طباعة باركود", icon=ft.Icons.QR_CODE_2, on_click=print_selected_barcodes),
                        ft.FilledButton(
                            "حذف المحدد", icon=ft.Icons.DELETE_OUTLINE,
                            style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE),
                            on_click=confirm_bulk_delete,
                        ),
                        ft.TextButton("إلغاء التحديد", on_click=lambda _: grid.clear_selection()),
                    ],
                    spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                bulk_bar.visible = True
            try:
                bulk_bar.update()
            except Exception:
                pass

        def confirm_bulk_delete(_=None) -> None:
            rows = list(selected_state["rows"])
            if not rows:
                return
            dialog = ft.AlertDialog(modal=True, title=ft.Text("تأكيد حذف المواد"))

            def yes(_ev=None) -> None:
                page.close(dialog)
                deleted = 0
                failures = 0
                for row in rows:
                    try:
                        ctx.items.delete(int(row.get("id") or 0))
                        deleted += 1
                    except Exception:
                        failures += 1
                grid.clear_selection()
                refresh()
                if failures:
                    notify(f"حُذفت {deleted} مادة، وتعذّر حذف {failures} لارتباطها بحركات أو فواتير", kind="warning")
                else:
                    notify(f"حُذفت {deleted} مادة", sound_kind="success")

            def no(_ev=None) -> None:
                page.close(dialog)

            dialog.content = ft.Text(
                f"سيتم حذف {len(rows)} مادة محددة. المواد المرتبطة بحركات مخزون أو فواتير لن تُحذف."
            )
            dialog.actions = [
                ft.TextButton("إلغاء", on_click=no),
                ft.FilledButton("حذف", on_click=yes, style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE)),
            ]
            page.open(dialog)

        def print_selected_barcodes(_=None) -> None:
            entries = [(row, 1) for row in selected_state["rows"]]
            if entries:
                open_barcode_print_dialog(entries)

        def toggle_density(_=None) -> None:
            compact = not density_state["compact"]
            density_state["compact"] = compact
            grid.set_row_height(36 if compact else 48)
            density_button.icon = ft.Icons.DENSITY_SMALL if compact else ft.Icons.DENSITY_MEDIUM
            density_button.tooltip = "عرض مريح" if compact else "عرض مضغوط"
            ctx.settings.set("ui.items_density", "compact" if compact else "comfortable")
            page.update()

        density_button = ft.IconButton(
            icon=ft.Icons.DENSITY_SMALL if density_state["compact"] else ft.Icons.DENSITY_MEDIUM,
            tooltip="عرض مريح" if density_state["compact"] else "عرض مضغوط",
            on_click=toggle_density,
        )
        bulk_bar = ft.Container(visible=False)

        grid = DataGrid(
            columns=ITEM_GRID_COLUMNS,
            rows=[],
            page=page,
            row_height=36 if density_state["compact"] else 48,
            hidden_keys=current_hidden(),
            on_cell_commit=_grid_commit,
            on_row_activate=lambda row: show_item_detail(row),
            on_sort=apply_grid_sort,
            selectable=True,
            on_selection_change=render_bulk_bar,
            on_row_menu=lambda row: [
                ("تعديل", ft.Icons.EDIT_OUTLINED, lambda r=row: open_item_editor(r)),
                ("طباعة باركود", ft.Icons.QR_CODE_2, lambda r=row: open_barcode_print_dialog([(r, 1)])),
                None,
                ("حذف", ft.Icons.DELETE_OUTLINE, lambda r=row: confirm_delete_item(r)),
            ],
            row_bg=item_row_bg,
            on_row_expand=item_expand_panel,
            on_search_shortcut=focus_search,
            on_new_shortcut=lambda: open_item_editor(),
            formatter=_grid_formatter,
            edit_formatter=lambda col, value, row: _edit_item_cell_text(col, value, row, ctx.settings),
            parser=lambda col, text: _parse_item_cell(col, text, ctx.settings),
            boxed=True,
            center_cells=True,
        )
        grid_more = ft.Container(visible=False)

        def filter_box(key: str, label: str, icon):
            box = ft.Container(
                ft.Row([ft.Icon(icon, size=15), ft.Text(label, size=11, weight=ft.FontWeight.W_600)], spacing=5),
                padding=ft.padding.symmetric(horizontal=11, vertical=8),
                border_radius=20, border=ft.border.all(1, Colors.BORDER), ink=True,
                on_click=lambda _, k=key: set_filter(k),
            )
            filter_boxes[key] = box
            return box

        def update_filter_styles():
            for key, box in filter_boxes.items():
                selected = key == filter_state["mode"]
                box.bgcolor = Colors.PRIMARY if selected else Colors.WHITE
                box.border = ft.border.all(1, Colors.PRIMARY if selected else Colors.BORDER)
                row = box.content
                if isinstance(row, ft.Row):
                    for control in row.controls:
                        if isinstance(control, ft.Text):
                            control.color = Colors.WHITE if selected else Colors.TEXT_MUTED
                        elif isinstance(control, ft.Icon):
                            control.color = Colors.WHITE if selected else Colors.TEXT_SECONDARY

        def set_filter(key: str):
            filter_state["mode"] = key
            render_limit["n"] = 60
            update_filter_styles()
            refresh()

        SORT_CHOICES = [
            ("name", "الاسم (أ-ي)"),
            ("price_desc", "السعر: الأعلى أولاً"),
            ("price_asc", "السعر: الأدنى أولاً"),
            ("qty_asc", "الكمية: الأقل أولاً"),
        ]
        sort_dd = ft.Dropdown(
            label="ترتيب حسب",
            value=sort_state["key"],
            options=[ft.dropdown.Option(key=k, text=label) for k, label in SORT_CHOICES],
            filled=True,
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.MD,
            border_color=Colors.BORDER,
            width=190,
        )

        def sort_changed(_=None):
            sort_state["key"] = sort_dd.value or "name"
            refresh()

        sort_dd.on_change = sort_changed

        # Definitions manager — a live-editable list (add/rename/delete) for
        # categories and units, replacing the old add-only dialog. A
        # BottomSheet rather than an AlertDialog, matching the pattern
        # already shipping for the item-detail view and main.py's "المزيد"
        # sheet. `categories`/`units` above are read by open_item_editor()'s
        # SearchSelect dropdowns via closure at call time (not at
        # definition time), so re-syncing those two variables here is
        # enough to keep the item editor's dropdowns fresh — no full
        # self.show_center() reload needed.
        definitions_tab = {"value": "categories"}
        definitions_sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)

        def sync_definitions_lists() -> None:
            nonlocal categories, units
            categories = ctx.definitions.list_categories()
            units = ctx.definitions.list_units()

        DEFAULT_UNIT_NAME = "قطعة"

        def ensure_default_unit_id() -> str | None:
            """Id of the "قطعة" unit, used to pre-select the base-unit field
            when adding a new material. Most stock is counted per piece, so
            defaulting to it saves a search-and-tap on every single add;
            "وحدة إضافية" and any other unit remain one tap away as before.
            Creates the unit once on first use if the shop hasn't defined
            any units yet, instead of leaving new items with a blank base
            unit until someone sets one up manually.
            """
            nonlocal units
            existing = next((u for u in units if (u.get("name") or "").strip() == DEFAULT_UNIT_NAME), None)
            if existing:
                return str(existing["id"])
            try:
                new_id = ctx.definitions.create_unit(DEFAULT_UNIT_NAME)
            except Exception:
                return None
            units = ctx.definitions.list_units()
            return str(new_id)

        def open_rename_definition(entry: dict, is_category: bool) -> None:
            name_field = SelectAllTextField(label="الاسم", value=entry["name"])
            abbr_field = SelectAllTextField(label="الاختصار", value=entry.get("abbreviation") or "") if not is_category else None
            dialog = ft.AlertDialog(modal=True)

            def close(_=None):
                page.close(dialog)

            def save(_=None):
                try:
                    if is_category:
                        ctx.definitions.rename_category(int(entry["id"]), name_field.value or "")
                    else:
                        ctx.definitions.rename_unit(int(entry["id"]), name_field.value or "", abbr_field.value if abbr_field else None)
                    close()
                    notify("تم التحديث")
                    render_definitions_sheet()
                except Exception as exc:
                    notify(str(exc), kind="error")

            dialog.title = ft.Text(f"تعديل {'التصنيف' if is_category else 'الوحدة'}")
            field_list: list[ft.Control] = [name_field] + ([abbr_field] if abbr_field else [])
            dialog.content = ft.Container(ft.Column(field_list, spacing=10, tight=True), width=380)
            dialog.actions = [ft.TextButton("إلغاء", on_click=close), ft.FilledButton("حفظ", icon=ft.Icons.SAVE_OUTLINED, on_click=save)]
            page.open(dialog)

        def confirm_delete_definition(entry: dict, is_category: bool) -> None:
            # Only reachable for entries with item_count == 0 (see
            # render_definitions_sheet — rows still in use aren't wrapped
            # in a Dismissible at all), but delete_category/delete_unit are
            # re-checked here too and any failure just restores the row via
            # render_definitions_sheet(), so a race is never destructive.
            label = "التصنيف" if is_category else "الوحدة"
            confirm = ft.AlertDialog(modal=True)

            def close(_=None):
                page.close(confirm)
                render_definitions_sheet()  # restores the swiped-away row

            def remove(_=None):
                try:
                    if is_category:
                        ctx.definitions.delete_category(int(entry["id"]))
                    else:
                        ctx.definitions.delete_unit(int(entry["id"]))
                    page.close(confirm)
                    notify(f"تم حذف {label}", kind="success", sound_kind="delete")
                    render_definitions_sheet()
                except Exception as exc:
                    page.close(confirm)
                    notify(str(exc), kind="error")
                    render_definitions_sheet()

            confirm.title = ft.Text(f"حذف {label}")
            confirm.content = ft.Text(f"هل تريد حذف «{entry['name']}»؟")
            confirm.actions = [ft.TextButton("إلغاء", on_click=close), ft.FilledButton("حذف", icon=ft.Icons.DELETE_FOREVER, on_click=remove)]
            page.open(confirm)

        def render_definitions_sheet() -> None:
            sync_definitions_lists()
            is_categories = definitions_tab["value"] == "categories"

            def set_tab(key: str) -> None:
                definitions_tab["value"] = key
                render_definitions_sheet()

            def tab_chip(label: str, key: str, count: int):
                active = definitions_tab["value"] == key
                return ft.Container(
                    ft.Text(f"{label} ({count})", size=12, color=Colors.WHITE if active else Colors.TEXT_MUTED, weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_500, text_align=ft.TextAlign.CENTER),
                    padding=ft.padding.symmetric(horizontal=14, vertical=9),
                    bgcolor=Colors.PRIMARY if active else Colors.WHITE,
                    border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
                    border_radius=14, on_click=lambda _, k=key: set_tab(k), ink=True, expand=True,
                )

            data = categories if is_categories else units
            row_controls: list[ft.Control] = []
            for entry in data:
                count = int(entry.get("item_count") or 0)
                in_use = count > 0
                subtitle = entry.get("abbreviation") if not is_categories and entry.get("abbreviation") else None
                badge = ft.Container(
                    ft.Text(f"{count} مادة" if in_use else "غير مستخدم", size=9, color=Colors.WARNING_DARK if in_use else Colors.TEXT_FAINT),
                    padding=ft.padding.symmetric(horizontal=8, vertical=3),
                    bgcolor=Colors.WARNING_BG if in_use else Colors.BACKGROUND, border_radius=10,
                )
                row_card = ft.Container(
                    ft.Row(
                        [
                            ft.Column(
                                [ft.Text(entry["name"], weight=ft.FontWeight.BOLD, size=13)] + ([ft.Text(subtitle, size=10, color=Colors.TEXT_SECONDARY)] if subtitle else []),
                                spacing=1, expand=True,
                            ),
                            badge,
                            inline_icon_button(ft.Icons.EDIT_OUTLINED, lambda _, e=entry, c=is_categories: open_rename_definition(e, c), tooltip="تعديل"),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8,
                    ),
                    padding=10, bgcolor=Colors.WHITE, border=ft.border.all(1, Colors.BORDER), border_radius=14, shadow=Shadow.SM,
                )
                if in_use:
                    # No delete affordance for entries still referenced by an
                    # item — matches the DB's own ON DELETE RESTRICT guard,
                    # so the UI never offers an action guaranteed to fail.
                    row_controls.append(row_card)
                else:
                    row_controls.append(
                        ft.Dismissible(
                            key=f"{'cat' if is_categories else 'unit'}-{entry['id']}",
                            content=row_card,
                            dismiss_direction=ft.DismissDirection.HORIZONTAL,
                            background=ft.Container(
                                content=ft.Row([ft.Icon(ft.Icons.DELETE_OUTLINE, color=Colors.WHITE, size=20), ft.Text("حذف", color=Colors.WHITE, size=11, weight=ft.FontWeight.W_600)], spacing=6, alignment=ft.MainAxisAlignment.START),
                                bgcolor=Colors.DANGER_DARK, border_radius=14, padding=ft.padding.symmetric(horizontal=18), alignment=ft.alignment.center_left,
                            ),
                            secondary_background=ft.Container(
                                content=ft.Row([ft.Text("حذف", color=Colors.WHITE, size=11, weight=ft.FontWeight.W_600), ft.Icon(ft.Icons.DELETE_OUTLINE, color=Colors.WHITE, size=20)], spacing=6, alignment=ft.MainAxisAlignment.END),
                                bgcolor=Colors.DANGER_DARK, border_radius=14, padding=ft.padding.symmetric(horizontal=18), alignment=ft.alignment.center_right,
                            ),
                            on_dismiss=lambda _, e=entry, c=is_categories: confirm_delete_definition(e, c),
                        )
                    )
            if not row_controls:
                row_controls.append(empty_state(
                    "لا توجد تصنيفات بعد" if is_categories else "لا توجد وحدات بعد",
                    icon=ft.Icons.SELL_OUTLINED if is_categories else ft.Icons.STRAIGHTEN,
                    hint="أضف أول عنصر من الحقل بالأسفل",
                ))

            new_name = SelectAllTextField(hint_text="تصنيف جديد…" if is_categories else "وحدة جديدة…", border_radius=20, content_padding=ft.padding.symmetric(horizontal=16, vertical=10), border_color=Colors.BORDER, expand=True)
            new_abbr = SelectAllTextField(hint_text="اختصار", border_radius=20, content_padding=ft.padding.symmetric(horizontal=14, vertical=10), border_color=Colors.BORDER, width=90) if not is_categories else None

            def add_new(_=None):
                try:
                    if is_categories:
                        ctx.definitions.create_category(new_name.value or "")
                    else:
                        ctx.definitions.create_unit(new_name.value or "", new_abbr.value if new_abbr else None)
                except Exception as exc:
                    notify(str(exc), kind="error")
                    return
                notify("تمت الإضافة")
                render_definitions_sheet()

            add_row_controls: list[ft.Control] = [new_name] + ([new_abbr] if new_abbr else []) + [
                ft.IconButton(ft.Icons.ADD_CIRCLE, icon_size=IconSize.HERO, icon_color=Colors.PRIMARY, tooltip="إضافة", on_click=add_new),
            ]
            add_card = ft.Container(
                ft.Column(
                    [
                        ft.Row(
                            [ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE, size=15, color=Colors.PRIMARY), ft.Text("إضافة تصنيف جديد" if is_categories else "إضافة وحدة جديدة", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_MUTED_DARK)],
                            spacing=6,
                        ),
                        ft.Row(add_row_controls, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ],
                    spacing=8,
                ),
                padding=12, bgcolor=Colors.BACKGROUND_ALT, border_radius=Radius.MD,
            )

            definitions_sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Row(
                            [
                                _icon_bubble(ft.Icons.SELL_OUTLINED if is_categories else ft.Icons.STRAIGHTEN, color=Colors.PRIMARY, bgcolor=Colors.PRIMARY_BG),
                                ft.Column(
                                    [
                                        ft.Text("التصنيفات والوحدات", size=17, weight=ft.FontWeight.BOLD),
                                        ft.Text(f"{len(categories)} تصنيف، {len(units)} وحدة قياس", size=11, color=Colors.TEXT_SECONDARY),
                                    ],
                                    spacing=1, expand=True,
                                ),
                                header_close_button(lambda _: page.close(definitions_sheet)),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12,
                        ),
                        ft.Row([tab_chip("التصنيفات", "categories", len(categories)), tab_chip("الوحدات", "units", len(units))], spacing=8),
                        ft.Container(ft.Column(row_controls, spacing=8, scroll=ft.ScrollMode.AUTO), height=280),
                        add_card,
                    ],
                    # tight + scroll (rather than a fixed pixel height on
                    # the outer Container, as this used to have): combined
                    # with the sheet's own is_scroll_controlled +
                    # maintain_bottom_view_insets_padding above, this lets
                    # the "تصنيف جديد…" field at the bottom rise above the
                    # on-screen keyboard on its own -- no height math to
                    # keep in sync here either. The 280px cap on the
                    # category/unit list itself is unrelated and stays:
                    # that one's just bounding a potentially long list, not
                    # working around the keyboard.
                    spacing=12, tight=True, scroll=ft.ScrollMode.AUTO,
                ),
                padding=ft.padding.only(left=18, right=18, top=12, bottom=20),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=28, top_right=28),
                shadow=Shadow.LG,
            )
            page.update()

        def open_definitions_sheet(_=None) -> None:
            render_definitions_sheet()
            page.open(definitions_sheet)

        def open_item_editor(item: dict | None = None, initial_barcode: str | None = None):
            name = SelectAllTextField(label="اسم المادة / الخدمة *", value=(item or {}).get("name", ""), autofocus=True)
            purchase = SmartAmountField(
                label=currency.amount_field_label("سعر الشراء / تكلفة الخدمة", self.ctx.settings),
                value=currency.to_input_text((item or {}).get("purchase_price", 0), self.ctx.settings),
            )
            selling = SmartAmountField(
                label=currency.amount_field_label("سعر البيع", self.ctx.settings),
                value=currency.to_input_text((item or {}).get("selling_price", 0), self.ctx.settings),
            )
            qty = SelectAllTextField(label="الرصيد الافتتاحي", value="0", keyboard_type=ft.KeyboardType.NUMBER, visible=item is None)
            # Live profit-margin readout: purely a display computed from
            # the two price fields above, nothing stored -- recalculated
            # on every keystroke in either field so staff can see the
            # margin they're setting instead of doing the math themselves
            # or discovering it later in the profitability report.
            margin_text = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)

            def update_margin(_=None) -> None:
                try:
                    buy = currency.parse_display_input(purchase.value, self.ctx.settings)
                    sell = currency.parse_display_input(selling.value, self.ctx.settings)
                except Exception:
                    margin_text.value = ""
                    margin_text.update()
                    return
                profit = sell - buy
                if sell > 0:
                    margin_pct = (profit / sell) * 100
                    margin_text.value = f"هامش الربح: {margin_pct:.1f}٪  •  الربح لكل وحدة: {self.money(profit)}"
                    margin_text.color = Colors.SUCCESS_DARK if profit >= 0 else Colors.DANGER
                else:
                    margin_text.value = ""
                margin_text.update()

            purchase.on_change = update_margin
            selling.on_change = update_margin
            # Prime the initial reading directly (no .update() -- these
            # controls aren't attached to the page yet at this point, same
            # reasoning as the barcode/checksum priming further below).
            try:
                _initial_buy = currency.parse_display_input(purchase.value, self.ctx.settings)
                _initial_sell = currency.parse_display_input(selling.value, self.ctx.settings)
                if _initial_sell > 0:
                    _initial_profit = _initial_sell - _initial_buy
                    margin_text.value = f"هامش الربح: {(_initial_profit / _initial_sell) * 100:.1f}٪  •  الربح لكل وحدة: {self.money(_initial_profit)}"
                    margin_text.color = Colors.SUCCESS_DARK if _initial_profit >= 0 else Colors.DANGER
            except Exception:
                pass
            barcode = SelectAllTextField(label="الباركود (اختياري)", value=(item or {}).get("barcode") or initial_barcode or "", expand=True)
            # Advisory only -- a EAN/UPC code with a bad check digit is
            # very likely a mistyped/mis-scanned code, but it never blocks
            # saving: some real-world stock genuinely carries odd/foreign
            # barcodes this check doesn't recognize.
            checksum_text = ft.Text("", size=10, color=Colors.WARNING_DARK)
            _checksum_enabled = barcode_settings.checksum_warning_enabled(ctx.settings)

            _KIND_LABELS = barcode_settings.KIND_LABELS

            def on_barcode_change(_=None) -> None:
                checksum_text.value = (barcode_quality.checksum_warning(barcode.value or "") or "") if _checksum_enabled else ""
                checksum_text.update()
                # Auto-detect and pre-select the matching barcode kind from
                # the code's own shape (see core.barcode_quality.detect_barcode_kind)
                # so the dropdown below only needs a manual touch when the
                # guess is wrong, instead of every single time.
                detected = barcode_quality.detect_barcode_kind(barcode.value or "")
                if detected and barcode_type.value != detected:
                    barcode_type.value = detected
                    barcode_type.update()
                barcode_type_hint.value = f"النوع المكتشف تلقائيًا: {_KIND_LABELS.get(detected, '')}" if detected else ""
                barcode_type_hint.update()

            barcode.on_change = on_barcode_change
            # Status shown *inside* the dialog, not via notify()/SnackBar.
            # Root cause found: this whole editor lives inside a modal
            # ft.AlertDialog. save() below always calls close() *before*
            # notify() -- that ordering is deliberate, not incidental: a
            # SnackBar raised while this dialog is still open renders behind
            # the dialog's modal scrim and is never visible to the user, with
            # no error. That silently ate every notify() call in
            # scan_for_editor (the "not configured" message, the exception
            # message, and an added confirmation message) -- which is why no
            # message of any kind was ever seen, even the failure ones. A
            # Text control placed directly in the dialog's own content can't
            # be hidden the same way, so status now goes there instead.
            scan_status = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)

            def log(step: str) -> None:
                print(f"[nano-scan] {step}", flush=True)

            async def scan_for_editor(_):
                if self.native_files is None:
                    log("native_files is None -- extension not attached")
                    scan_status.value = "مسح الباركود غير مهيأ في هذا البناء"
                    scan_status.color = Colors.DANGER
                    scan_status.update()
                    return
                scan_status.value = "جارٍ فتح الكاميرا..."
                scan_status.color = Colors.TEXT_SECONDARY
                scan_status.update()
                log("calling native_files.scan_barcode()")
                try:
                    code = await self.native_files.scan_barcode()
                except Exception as exc:
                    log(f"ERROR from scan_barcode(): {exc!r}")
                    scan_status.value = str(exc)
                    scan_status.color = Colors.DANGER
                    scan_status.update()
                    return
                log(f"scan_barcode() returned: {code!r}")
                if code:
                    barcode.value = code
                    barcode.update()
                    scan_status.value = f"تم قراءة الباركود: {code}"
                    scan_status.color = Colors.SUCCESS
                    scan_status.update()
                    page.update()
                else:
                    # User backed out of the scanner, or the camera timed
                    # out / permission was denied without raising -- make
                    # this visible instead of failing silently, since that's
                    # indistinguishable from "the field didn't update" to
                    # someone testing this on-device.
                    scan_status.value = "لم تتم قراءة أي باركود (تم الإلغاء أو رفض إذن الكاميرا)"
                    scan_status.color = Colors.DANGER
                    scan_status.update()

            # Random-barcode generator: lets staff assign a barcode to items
            # that don't already have a printed one, without needing a
            # physical code to scan. EAN-13 uses the standard mod-10 check
            # digit so generated codes are valid to print/scan later;
            # Code128/QR have no fixed length or checksum, so we just emit a
            # random alphanumeric token of a sane length for those.
            barcode_type = SearchSelect(
                label="نوع الباركود (عند التوليد)",
                choices=[("EAN13", "EAN-13"), ("CODE128", "Code 128"), ("QR", "QR")],
                value=barcode_quality.detect_barcode_kind(barcode.value) or barcode_settings.default_kind(ctx.settings),
                allow_clear=False,
            )
            _initial_detected = barcode_quality.detect_barcode_kind(barcode.value)
            barcode_type_hint = ft.Text(
                f"النوع المكتشف تلقائيًا: {_KIND_LABELS.get(_initial_detected, '')}" if _initial_detected else "",
                size=10, color=Colors.TEXT_FAINT,
            )
            # Initial value primed directly above (not via on_barcode_change,
            # which calls .update() on controls -- not yet safe to do before
            # this sheet's content tree is actually attached to the page).
            checksum_text.value = (barcode_quality.checksum_warning(barcode.value or "") or "") if _checksum_enabled else ""

            def generate_random_barcode(_):
                kind = barcode_type.value or barcode_settings.default_kind(ctx.settings)
                prefix = barcode_settings.internal_prefix(ctx.settings) if kind == "EAN13" else None
                barcode.value = barcode_quality.generate_barcode_value(kind, prefix=prefix)
                on_barcode_change()
                page.update()

            scan_button = ft.IconButton(icon=ft.Icons.QR_CODE_SCANNER, tooltip="مسح الباركود بالكاميرا", on_click=scan_for_editor, visible=supports("barcode_camera_scan"))
            generate_button = ft.IconButton(icon=ft.Icons.CASINO_OUTLINED, tooltip="توليد باركود عشوائي", on_click=generate_random_barcode)
            barcode_row = ft.Row([barcode, scan_button, generate_button], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)
            barcode_status_row = ft.Row([scan_status])
            barcode_type_row = ft.Column(
                [ft.Row([ft.Container(barcode_type, width=220)], spacing=6), barcode_type_hint],
                spacing=2,
            )

            # Secondary barcodes (e.g. a separate code printed on the
            # carton vs the single piece): only meaningful once the item
            # actually has an id to attach them to, so this section is
            # edit-mode only -- a new item shows a hint instead and the
            # staff member adds alternates after the first save.
            extra_status = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
            extra_list_column = ft.Column(spacing=4)

            def render_extra_barcodes() -> None:
                rows = ctx.items.list_barcodes(int(item["id"])) if item else []
                controls = []
                for row in rows:
                    unit_label = f" — {row['unit_name']}" if row.get("unit_name") else ""
                    label_suffix = f" ({row['label']})" if row.get("label") else ""
                    controls.append(
                        ft.Row(
                            [
                                ft.Text(f"{row['barcode']}{unit_label}{label_suffix}", size=12, expand=True),
                                inline_icon_button(ft.Icons.DELETE_OUTLINE, lambda _, rid=row["id"]: remove_extra_barcode(rid), color=Colors.DANGER),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        )
                    )
                extra_list_column.controls = controls
                # This is called once during editor construction (line ~668
                # below), *before* the sheet's content tree is attached to
                # the page -- calling .update() on a control that isn't on
                # the page yet raises, which was silently aborting the
                # whole open_item_editor() build for every existing item
                # (the "تعديل" button appeared to do nothing at all). Only
                # refresh here if this control is actually already on the
                # page, i.e. this is a later re-render after add/remove.
                if extra_list_column.page is not None:
                    extra_list_column.update()

            def remove_extra_barcode(row_id: int) -> None:
                ctx.items.remove_barcode(row_id)
                render_extra_barcodes()

            new_alt_barcode = SelectAllTextField(label="باركود إضافي (قطعة/كرتون)", expand=True)
            new_alt_unit = SearchSelect(label="للوحدة (اختياري)", choices=[(str(u["id"]), u["name"]) for u in units])
            alt_checksum_text = ft.Text("", size=10, color=Colors.WARNING_DARK)

            def on_alt_barcode_change(_=None) -> None:
                alt_checksum_text.value = (barcode_quality.checksum_warning(new_alt_barcode.value or "") or "") if _checksum_enabled else ""
                alt_checksum_text.update()

            new_alt_barcode.on_change = on_alt_barcode_change

            def generate_alt_barcode(_=None) -> None:
                kind = barcode_type.value or barcode_settings.default_kind(ctx.settings)
                prefix = barcode_settings.internal_prefix(ctx.settings) if kind == "EAN13" else None
                new_alt_barcode.value = barcode_quality.generate_barcode_value(kind, prefix=prefix)
                new_alt_barcode.update()
                on_alt_barcode_change()

            async def scan_for_alt_barcode(_=None) -> None:
                if self.native_files is None:
                    extra_status.value = "مسح الباركود غير مهيأ في هذا البناء"
                    extra_status.color = Colors.DANGER
                    extra_status.update()
                    return
                extra_status.value = "جارٍ فتح الكاميرا..."
                extra_status.color = Colors.TEXT_SECONDARY
                extra_status.update()
                try:
                    code = await self.native_files.scan_barcode()
                except Exception as exc:
                    extra_status.value = str(exc)
                    extra_status.color = Colors.DANGER
                    extra_status.update()
                    return
                if code:
                    new_alt_barcode.value = code
                    new_alt_barcode.update()
                    on_alt_barcode_change()
                    extra_status.value = f"تم قراءة الباركود: {code}"
                    extra_status.color = Colors.SUCCESS
                    extra_status.update()
                else:
                    extra_status.value = "لم تتم قراءة أي باركود (تم الإلغاء أو رفض إذن الكاميرا)"
                    extra_status.color = Colors.DANGER
                    extra_status.update()

            def add_extra_barcode(_=None) -> None:
                code = (new_alt_barcode.value or "").strip()
                if not code:
                    return

                def do_add() -> None:
                    try:
                        ctx.items.add_barcode(
                            int(item["id"]), code,
                            unit_id=int(new_alt_unit.value) if new_alt_unit.value else None,
                        )
                        new_alt_barcode.value = ""
                        new_alt_unit.value = None
                        extra_status.value = ""
                        new_alt_barcode.update(); new_alt_unit.update(); extra_status.update()
                        render_extra_barcodes()
                    except Exception as exc:
                        extra_status.value = str(exc)
                        extra_status.color = Colors.DANGER
                        extra_status.update()

                if barcode_settings.similar_warning_enabled(ctx.settings):
                    similar = ctx.items.find_similar_barcodes(code, exclude_item_id=int(item["id"]))
                    if similar:
                        _confirm_similar_barcode(page, similar, do_add)
                        return
                if _checksum_enabled:
                    warn = barcode_quality.checksum_warning(code)
                    if warn:
                        extra_status.value = warn
                        extra_status.color = Colors.WARNING_DARK
                        extra_status.update()
                do_add()

            extra_barcodes_section: ft.Control
            if item:
                render_extra_barcodes()
                extra_barcodes_section = ft.Column(
                    [
                        ft.Text("باركودات إضافية (قطعة/كرتون/كود قديم) — لكل وحدة باركود مستقل", size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY),
                        extra_list_column,
                        ft.Row(
                            [
                                new_alt_barcode,
                                ft.IconButton(icon=ft.Icons.QR_CODE_SCANNER, tooltip="مسح الباركود بالكاميرا", on_click=scan_for_alt_barcode, visible=supports("barcode_camera_scan")),
                                ft.IconButton(icon=ft.Icons.CASINO_OUTLINED, tooltip="توليد باركود عشوائي", on_click=generate_alt_barcode),
                            ],
                            spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        alt_checksum_text,
                        ft.Row([new_alt_unit, ft.IconButton(icon=ft.Icons.ADD_CIRCLE_OUTLINE, tooltip="إضافة", on_click=add_extra_barcode)], spacing=6),
                        extra_status,
                    ],
                    spacing=6,
                )
            else:
                extra_barcodes_section = ft.Text(
                    "يمكنك إضافة باركودات إضافية (قطعة/كرتون) بعد حفظ المادة أول مرة.",
                    size=10, color=Colors.TEXT_FAINT,
                )
            kind = SearchSelect(label="النوع", choices=[("مخزون", "مخزون"), ("خدمة", "خدمة")], value=(item or {}).get("item_type") or "مخزون", allow_clear=False)
            category = SearchSelect(label="التصنيف", choices=[(str(c["id"]), c["name"]) for c in categories], value=str(item.get("category_id")) if item and item.get("category_id") else None)
            default_unit_id = str(item.get("base_unit_id")) if item and item.get("base_unit_id") else (None if item else ensure_default_unit_id())
            base_unit = SearchSelect(label="الوحدة الأساسية", choices=[(str(u["id"]), u["name"]) for u in units], value=default_unit_id)
            current_units = ctx.items.units(int(item["id"])) if item else []
            existing_barcodes = ctx.items.list_barcodes(int(item["id"])) if item else []
            barcode_by_unit: dict[int, dict] = {}
            for _b in existing_barcodes:
                if _b.get("unit_id") is not None:
                    barcode_by_unit[int(_b["unit_id"])] = _b

            # Sub-units: the old form capped this at ONE alternate unit (a
            # single alt_unit/alt_factor pair) even though the DB
            # (item_units) always supported many. Now it's a repeatable list
            # of rows -- each row is a unit + conversion factor + its own
            # barcode (typed, scanned, or auto-generated), with add/remove.
            alt_rows_state: list[dict] = []
            alt_rows_column = ft.Column(spacing=6, tight=True)
            alt_status = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
            _alt_checksum_enabled = barcode_settings.checksum_warning_enabled(ctx.settings)

            def _remove_alt_row(state: dict) -> None:
                if state in alt_rows_state:
                    alt_rows_state.remove(state)
                    alt_rows_column.controls.remove(state["card"])
                    alt_rows_column.update()

            def _add_alt_row(unit_id=None, factor=1.0, barcode="", selling_price=None, purchase_price=None) -> dict:
                unit_sel = SearchSelect(label="الوحدة الفرعية", choices=[(str(u["id"]), u["name"]) for u in units], value=str(unit_id) if unit_id else None)
                factor_f = SelectAllTextField(label="معامل التحويل", value=str(factor), keyboard_type=ft.KeyboardType.NUMBER)
                price_f = SmartAmountField(
                    label=currency.amount_field_label("سعر البيع (اختياري)", self.ctx.settings),
                    value=currency.to_input_text(float(selling_price), self.ctx.settings) if selling_price not in (None, "") else "",
                )
                purchase_price_f = SmartAmountField(
                    label=currency.amount_field_label("سعر الشراء (اختياري)", self.ctx.settings),
                    value=currency.to_input_text(float(purchase_price), self.ctx.settings) if purchase_price not in (None, "") else "",
                )
                bc_field = SelectAllTextField(label="الباركود (اختياري)", value=barcode or "", expand=True)
                cs_text = ft.Text("", size=10, color=Colors.WARNING_DARK)

                def _on_bc_change(_=None) -> None:
                    cs_text.value = (barcode_quality.checksum_warning(bc_field.value or "") or "") if _alt_checksum_enabled else ""
                    cs_text.update()
                bc_field.on_change = _on_bc_change
                cs_text.value = (barcode_quality.checksum_warning(bc_field.value or "") or "") if _alt_checksum_enabled else ""

                def _gen_bc(_=None) -> None:
                    kind = barcode_type.value or barcode_settings.default_kind(ctx.settings)
                    prefix = barcode_settings.internal_prefix(ctx.settings) if kind == "EAN13" else None
                    bc_field.value = barcode_quality.generate_barcode_value(kind, prefix=prefix)
                    _on_bc_change()
                    bc_field.update()

                async def _scan_bc(_=None) -> None:
                    if self.native_files is None:
                        alt_status.value = "مسح الباركود غير مهيأ في هذا البناء"
                        alt_status.color = Colors.DANGER
                        alt_status.update()
                        return
                    alt_status.value = "جارٍ فتح الكاميرا..."
                    alt_status.color = Colors.TEXT_SECONDARY
                    alt_status.update()
                    try:
                        code = await self.native_files.scan_barcode()
                    except Exception as exc:
                        alt_status.value = str(exc)
                        alt_status.color = Colors.DANGER
                        alt_status.update()
                        return
                    if code:
                        bc_field.value = code
                        _on_bc_change()
                        bc_field.update()
                        alt_status.value = f"تم قراءة الباركود: {code}"
                        alt_status.color = Colors.SUCCESS
                        alt_status.update()
                    else:
                        alt_status.value = "لم تتم قراءة أي باركود (تم الإلغاء أو رفض إذن الكاميرا)"
                        alt_status.color = Colors.DANGER
                        alt_status.update()

                state: dict = {"unit": unit_sel, "factor": factor_f, "price": price_f, "purchase_price": purchase_price_f, "barcode": bc_field, "checksum": cs_text}
                card = ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Container(unit_sel, expand=True),
                                    ft.Container(factor_f, width=110),
                                    inline_icon_button(ft.Icons.DELETE_OUTLINE, lambda _, s=state: _remove_alt_row(s), color=Colors.DANGER),
                                ],
                                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Row(
                                [
                                    ft.Container(price_f, width=150),
                                    ft.Container(purchase_price_f, width=150),
                                    ft.Container(
                                        ft.Text("سعر الشراء يُستخدم في فواتير الشراء فقط", size=10, color=Colors.TEXT_SECONDARY),
                                        expand=True,
                                    ),
                                ],
                                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Row(
                                [
                                    ft.Container(bc_field, expand=True),
                                    ft.IconButton(icon=ft.Icons.QR_CODE_SCANNER, tooltip="مسح الباركود بالكاميرا", on_click=_scan_bc, visible=supports("barcode_camera_scan")),
                                    ft.IconButton(icon=ft.Icons.CASINO_OUTLINED, tooltip="توليد باركود عشوائي", on_click=_gen_bc),
                                ],
                                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            cs_text,
                        ],
                        spacing=5, tight=True,
                    ),
                    padding=ft.padding.all(8),
                    border=ft.border.all(1, Colors.BORDER_ALT),
                    border_radius=Radius.MD,
                    bgcolor=Colors.WHITE,
                )
                state["card"] = card
                alt_rows_state.append(state)
                alt_rows_column.controls.append(card)
                return state

            # Prefill existing sub-units (and any per-unit barcode already
            # registered for them) so editing round-trips cleanly.
            for _cu in current_units:
                if _cu.get("is_base"):
                    continue
                _bid = int(_cu["id"])
                _brow = barcode_by_unit.get(_bid)
                _add_alt_row(unit_id=_bid, factor=float(_cu.get("conversion_factor") or 1), barcode=(_brow or {}).get("barcode") or "", selling_price=_cu.get("selling_price"), purchase_price=_cu.get("purchase_price"))

            def _add_alt_row_btn(_=None) -> None:
                _add_alt_row()
                alt_rows_column.update()

            # Paged bottom sheet -- one focused step per screen (basic
            # info / barcode / price & stock / units) with a dot
            # indicator and Back/Next footer, instead of every section
            # stacked into a single tall scrolling form. Same pattern
            # already used for the QR item-card sheet below
            # (open_item_card_sheet) via _open_paged_sheet, adopted here
            # too so the two sheets behave consistently. "إضافة مادة
            # جديدة" was the tallest case in the old layout (it shows the
            # extra "الرصيد الافتتاحي" field that edit-mode hides), so
            # splitting it into steps also keeps each screen comfortably
            # clear of the on-screen keyboard.
            nav_ref: dict = {"nav": None}

            def perform_save() -> None:
                try:
                    alternate_units = []
                    pending_barcodes: list[dict] = []
                    for _row in alt_rows_state:
                        _uid = _row["unit"].value
                        if not _uid:
                            continue
                        _sp_raw = (_row["price"].value or "").strip()
                        _sp_val = currency.parse_display_input(_sp_raw, self.ctx.settings) if _sp_raw else None
                        _pp_raw = (_row["purchase_price"].value or "").strip()
                        _pp_val = currency.parse_display_input(_pp_raw, self.ctx.settings) if _pp_raw else None
                        alternate_units.append({"unit_id": int(_uid), "conversion_factor": float(_row["factor"].value or 1), "selling_price": _sp_val, "purchase_price": _pp_val})
                        _code = (_row["barcode"].value or "").strip()
                        if _code:
                            pending_barcodes.append({"code": _code, "unit_id": int(_uid)})
                    kwargs = dict(
                        name=name.value or "", item_type=kind.value or "مخزون",
                        category_id=int(category.value) if category.value else None,
                        purchase_price=currency.parse_display_input(purchase.value, self.ctx.settings),
                        selling_price=currency.parse_display_input(selling.value, self.ctx.settings),
                        base_unit_id=int(base_unit.value) if base_unit.value else None, item_units=alternate_units,
                        barcode=barcode.value or None,
                    )
                    if item:
                        ctx.items.update(int(item["id"]), **kwargs)
                        item_id = int(item["id"])
                        msg = "تم تحديث المادة"
                    else:
                        item_id = ctx.items.create(quantity=float(qty.value or 0), **kwargs)
                        msg = "تمت إضافة المادة"
                    # Persist each sub-unit's barcode (idempotent -- skip any
                    # code the item already owns; surface conflicts with other
                    # items without aborting the whole save).
                    _owned = {(kwargs.get("barcode") or "").strip()}
                    _owned |= {(_b.get("barcode") or "").strip() for _b in ctx.items.list_barcodes(item_id)}
                    _warnings: list[str] = []
                    for _pb in pending_barcodes:
                        if _pb["code"] in _owned:
                            continue
                        try:
                            ctx.items.add_barcode(item_id, _pb["code"], unit_id=_pb["unit_id"])
                            _owned.add(_pb["code"])
                        except Exception as _exc:
                            _warnings.append(str(_exc))
                    nav_ref["nav"].close()
                    if _warnings:
                        notify(msg + " — " + "؛ ".join(dict.fromkeys(_warnings)))
                    else:
                        notify(msg)
                    refresh()
                except Exception as exc:
                    # notify() -> SnackBar is invisible here: this sheet is
                    # still open when save() fails, same root cause as the
                    # barcode scan messages above. Reuse the sheet's own
                    # status line instead so failures like a duplicate
                    # barcode are actually seen.
                    scan_status.value = str(exc)
                    scan_status.color = Colors.DANGER
                    scan_status.update()

            def _check_barcode_then_save() -> None:
                # Auto-assign a barcode for items saved without one, when the
                # admin has opted into it (core.barcode_settings) -- one less
                # manual step for staff who don't have a printed code yet.
                if not (barcode.value or "").strip() and barcode_settings.auto_generate_enabled(ctx.settings):
                    kind = barcode_type.value or barcode_settings.default_kind(ctx.settings)
                    prefix = barcode_settings.internal_prefix(ctx.settings) if kind == "EAN13" else None
                    barcode.value = barcode_quality.generate_barcode_value(kind, prefix=prefix)
                code = (barcode.value or "").strip()
                if code and barcode_settings.similar_warning_enabled(ctx.settings):
                    exclude_id = int(item["id"]) if item else None
                    similar = ctx.items.find_similar_barcodes(code, exclude_item_id=exclude_id)
                    if similar:
                        _confirm_similar_barcode(page, similar, perform_save)
                        return
                perform_save()

            def save(_=None):
                nm = (name.value or "").strip()
                if nm:
                    exclude_id = int(item["id"]) if item else None
                    similar_names = ctx.items.find_similar_names(nm, exclude_item_id=exclude_id)
                    if similar_names:
                        _confirm_similar_name(page, similar_names, _check_barcode_then_save)
                        return
                _check_barcode_then_save()

            _STEP_TITLES = ["البيانات الأساسية", "الباركود", "السعر والمخزون", "الوحدات الفرعية"]
            _STEP_COUNT = len(_STEP_TITLES)

            def render_page(idx: int, nav):
                nav_ref["nav"] = nav
                step_label = f"الخطوة {['١', '٢', '٣', '٤'][idx]} من ٤ — {_STEP_TITLES[idx]}"

                if idx == 0:
                    body = ft.Column(
                        [
                            name,
                            ft.ResponsiveRow([
                                ft.Container(kind, col={"xs": 6}), ft.Container(category, col={"xs": 6}),
                            ], spacing=7, run_spacing=7),
                        ],
                        spacing=9, tight=True,
                    )
                    footer = [
                        ft.TextButton("إلغاء", on_click=lambda _: nav.close()),
                        ft.FilledButton("التالي", icon=ft.Icons.ARROW_BACK_ROUNDED, on_click=lambda _: nav.next()),
                    ]
                    return step_label, body, footer

                if idx == 1:
                    body = ft.Column(
                        [barcode_row, checksum_text, barcode_status_row, barcode_type_row, extra_barcodes_section],
                        spacing=9, tight=True,
                    )
                    footer = [
                        ft.TextButton("رجوع", icon=ft.Icons.ARROW_FORWARD_ROUNDED, on_click=lambda _: nav.back()),
                        ft.FilledButton("التالي", icon=ft.Icons.ARROW_BACK_ROUNDED, on_click=lambda _: nav.next()),
                    ]
                    return step_label, body, footer

                if idx == 2:
                    body = ft.Column(
                        [
                            ft.ResponsiveRow([
                                ft.Container(purchase, col={"xs": 6, "md": 4}),
                                ft.Container(selling, col={"xs": 6, "md": 4}),
                                ft.Container(qty, col={"xs": 12, "md": 4}),
                            ], spacing=7, run_spacing=7),
                            margin_text,
                            ft.Text("الخدمات لا تؤثر في المخزون، وتستخدم تكلفة الخدمة لحساب الربحية.", size=10, color=Colors.TEXT_SECONDARY),
                        ],
                        spacing=9, tight=True,
                    )
                    footer = [
                        ft.TextButton("رجوع", icon=ft.Icons.ARROW_FORWARD_ROUNDED, on_click=lambda _: nav.back()),
                        ft.FilledButton("التالي", icon=ft.Icons.ARROW_BACK_ROUNDED, on_click=lambda _: nav.next()),
                    ]
                    return step_label, body, footer

                body = ft.Column(
                    [
                        ft.Text("الوحدة الأساسية ثم الوحدات الفرعية — لكل وحدة معامل تحويل وباركود وسعر بيع اختياري", size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY),
                        ft.Container(base_unit, width=220),
                        alt_rows_column,
                        ft.Row(
                            [
                                ft.OutlinedButton("إضافة وحدة فرعية", icon=ft.Icons.ADD_CIRCLE_OUTLINE, on_click=_add_alt_row_btn),
                            ],
                            spacing=6,
                        ),
                        alt_status,
                    ],
                    spacing=9, tight=True,
                )
                footer = [
                    ft.TextButton("رجوع", icon=ft.Icons.ARROW_FORWARD_ROUNDED, on_click=lambda _: nav.back()),
                    ft.FilledButton("حفظ", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
                ]
                return step_label, body, footer

            _open_paged_sheet(
                page,
                icon=ft.Icons.INVENTORY_2_OUTLINED, icon_color=Colors.PRIMARY, icon_bg=Colors.PRIMARY_BG,
                title="تعديل المادة" if item else "مادة / خدمة جديدة",
                page_count=_STEP_COUNT, render_page=render_page,
            )

        def open_barcode_print_dialog(
            entries: list[tuple[dict, int]],
            *,
            layout: str = "sheet",
            roll_width_mm: int | None = None,
        ) -> None:
            """entries: [(item_dict, copies), ...] for items that already have a barcode.

            ``layout``/``roll_width_mm`` come from the printer-type chip in
            open_bulk_barcode_sheet() above (falling back to the admin's saved
            default from core.barcode_settings when called directly, e.g. from
            a single-item action elsewhere) -- see barcode_labels_html()'s
            docstring for what each layout actually renders.
            """
            usable = [(it, max(1, int(n or 1))) for it, n in entries if str(it.get("barcode") or "").strip()]
            if not usable:
                notify("لا توجد مواد لها باركود ضمن التحديد")
                return
            total = sum(n for _, n in usable)
            is_roll = layout == "roll"
            roll_width_mm = roll_width_mm or barcode_settings.label_roll_width_mm(ctx.settings)
            dialog = ft.AlertDialog(modal=True)
            price_qr_toggle = ft.Checkbox(
                label="إضافة رمز QR للسعر (يمسحه الزبون بكاميرا هاتفه)",
                value=barcode_settings.label_price_qr_default(ctx.settings),
            )
            columns_field = SearchSelect(
                label="عدد الأعمدة",
                choices=[("2", "٢ أعمدة"), ("3", "٣ أعمدة"), ("4", "٤ أعمدة")],
                value=str(barcode_settings.label_columns(ctx.settings)),
                allow_clear=False,
            )
            printer_note = ft.Container(
                ft.Row(
                    [
                        ft.Icon(ft.Icons.RECEIPT_LONG_OUTLINED if is_roll else ft.Icons.APPS_ROUNDED, size=16, color=Colors.PURPLE),
                        ft.Text(
                            f"طابعة حرارية — لفة {roll_width_mm} مم، ملصق واحد لكل صفحة" if is_roll else "ورق A4 — عدة ملصقات في كل صفحة",
                            size=11, color=Colors.TEXT_MUTED_DARK,
                        ),
                    ],
                    spacing=6,
                ),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                bgcolor=Colors.PURPLE_BG, border_radius=Radius.SM,
            )

            def close(_=None):
                page.close(dialog)

            def build_html() -> str:
                labels = []
                for it, copies in usable:
                    for _ in range(copies):
                        labels.append({"name": it.get("name"), "barcode": it.get("barcode"), "price": it.get("selling_price")})
                try:
                    columns = int(columns_field.value or barcode_settings.label_columns(ctx.settings))
                except ValueError:
                    columns = barcode_settings.label_columns(ctx.settings)
                return ctx.documents.barcode_labels_html(
                    labels,
                    columns=columns,
                    include_price_qr=price_qr_toggle.value,
                    label_size=barcode_settings.label_dimensions(ctx.settings),
                    show_text=barcode_settings.label_show_text(ctx.settings),
                    layout=layout,
                    roll_width_mm=roll_width_mm,
                )

            async def do_print(_):
                close()
                if self.native_files is None:
                    notify("الطباعة الأصلية غير مهيأة في هذا البناء")
                    return
                try:
                    await self.native_files.print_html(build_html(), name="nano-barcode-labels")
                except Exception as exc:
                    notify(str(exc), kind="error")

            async def do_pdf(_):
                close()
                if self.native_files is None:
                    notify("تصدير PDF غير مهيأ في هذا البناء")
                    return
                try:
                    await self.native_files.share_pdf(build_html(), filename="nano_barcode_labels.pdf")
                except Exception as exc:
                    notify(str(exc), kind="error")

            surface_label = f"لفة حرارية {roll_width_mm} مم" if is_roll else "ورق A4"
            dialog.title = ft.Text("طباعة ملصقات الباركود")
            dialog.content = ft.Column(
                [
                    ft.Text(f"سيتم إنشاء {total} ملصق باركود لِـ {len(usable)} مادة على {surface_label}."),
                    printer_note,
                    *([columns_field] if not is_roll else []),
                    price_qr_toggle,
                ],
                spacing=8, tight=True,
            )
            dialog.actions = [
                ft.TextButton("إلغاء", on_click=close),
                ft.OutlinedButton("تصدير PDF", icon=ft.Icons.DESCRIPTION_OUTLINED, on_click=do_pdf),
                ft.FilledButton("طباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=do_print),
            ]
            page.open(dialog)

        def open_item_card_sheet(item: dict) -> None:
            """Issue a signed QR 'item card' for one item -- a two-page
            sheet (choose what to include, then share) instead of one
            cluttered form, matching the design agreed on above."""
            include_selling = ft.Checkbox(label="تضمين سعر البيع", value=True)
            include_purchase = ft.Checkbox(label="تضمين سعر الشراء (يكشف تكلفتك لمن يمسح الرمز)", value=False)
            include_category = ft.Checkbox(label="تضمين التصنيف", value=bool(item.get("category_name")))
            include_unit = ft.Checkbox(label="تضمين الوحدة", value=True)
            size_text = ft.Text("", size=10, color=Colors.TEXT_FAINT)

            item_units = ctx.items.units(int(item["id"])) if item.get("id") else []
            base_unit_row = next((u for u in item_units if u.get("is_base")), None)
            base_unit_name = (base_unit_row or {}).get("name")

            def build_payload() -> str:
                return item_card_signing.build_item_card(
                    ctx.db,
                    name=item.get("name") or "",
                    barcode=item.get("barcode") or "",
                    purchase_price=float(item.get("purchase_price") or 0) if include_purchase.value else None,
                    selling_price=float(item.get("selling_price") or 0) if include_selling.value else None,
                    unit=base_unit_name if include_unit.value else None,
                    category=item.get("category_name") if include_category.value else None,
                )

            def size_reading(payload: str) -> tuple[str, str]:
                n = len(payload.encode("utf-8"))
                color = Colors.WARNING_DARK if n > 200 else Colors.TEXT_FAINT
                return f"الحجم التقريبي: {n} بايت — كلما قلّت الحقول كان المسح أسرع وأوثق", color

            def on_option_change(_=None):
                size_text.value, size_text.color = size_reading(build_payload())
                size_text.update()

            for cb in (include_selling, include_purchase, include_category, include_unit):
                cb.on_change = on_option_change

            async def do_print(_=None):
                try:
                    html = ctx.documents.item_card_html(
                        payload=build_payload(), name=item.get("name") or "", barcode=item.get("barcode"),
                        purchase_price=float(item.get("purchase_price") or 0) if include_purchase.value else None,
                        selling_price=float(item.get("selling_price") or 0) if include_selling.value else None,
                        unit=base_unit_name if include_unit.value else None,
                        category=item.get("category_name") if include_category.value else None,
                    )
                    if self.native_files is None:
                        notify("الطباعة الأصلية غير مهيأة في هذا البناء"); return
                    await self.native_files.print_html(html, name="nano-item-card")
                except Exception as exc:
                    notify(str(exc), kind="error")

            async def do_pdf(_=None):
                try:
                    html = ctx.documents.item_card_html(
                        payload=build_payload(), name=item.get("name") or "", barcode=item.get("barcode"),
                        purchase_price=float(item.get("purchase_price") or 0) if include_purchase.value else None,
                        selling_price=float(item.get("selling_price") or 0) if include_selling.value else None,
                        unit=base_unit_name if include_unit.value else None,
                        category=item.get("category_name") if include_category.value else None,
                    )
                    if self.native_files is None:
                        notify("تصدير PDF غير مهيأ في هذا البناء"); return
                    await self.native_files.share_pdf(html, filename=f"nano_item_card_{item.get('id')}.pdf")
                except Exception as exc:
                    notify(str(exc), kind="error")

            def render_page(idx: int, nav):
                if idx == 0:
                    # Initial value primed directly (no .update()) -- same
                    # reasoning as the margin/checksum priming in the item
                    # editor above: these controls aren't attached to the
                    # page yet at this point.
                    size_text.value, size_text.color = size_reading(build_payload())
                    body = ft.Column(
                        [
                            ft.Text(f"{item.get('name')}", weight=ft.FontWeight.BOLD),
                            ft.Text("اختر البيانات التي تُضمَّن داخل رمز البطاقة قبل مشاركتها", size=11, color=Colors.TEXT_SECONDARY),
                            include_selling, include_purchase, include_category, include_unit,
                            size_text,
                        ],
                        spacing=6, tight=True,
                    )
                    footer = [
                        ft.TextButton("إلغاء", on_click=lambda _: nav.close()),
                        ft.FilledButton("التالي", icon=ft.Icons.ARROW_BACK_ROUNDED, on_click=lambda _: nav.next()),
                    ]
                    return "الخطوة ١ من ٢ — اختيار البيانات", body, footer

                summary_rows = [ft.Text(item.get("name") or "", weight=ft.FontWeight.BOLD)]
                if item.get("barcode"):
                    summary_rows.append(ft.Text(f"الباركود: {item.get('barcode')}", size=11, color=Colors.TEXT_SECONDARY))
                if include_selling.value:
                    summary_rows.append(
                        ft.Row(
                            [
                                ft.Text("سعر البيع: ", size=11, color=Colors.TEXT_SECONDARY),
                                ft.Text(self.money(item.get("selling_price")), size=11, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                            ],
                            spacing=0, tight=True,
                        )
                    )
                if include_purchase.value:
                    summary_rows.append(
                        ft.Row(
                            [
                                ft.Text("سعر الشراء: ", size=11, color=Colors.TEXT_SECONDARY),
                                ft.Text(self.money(item.get("purchase_price")), size=11, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                            ],
                            spacing=0, tight=True,
                        )
                    )
                if include_category.value and item.get("category_name"):
                    summary_rows.append(ft.Text(f"التصنيف: {item.get('category_name')}", size=11, color=Colors.TEXT_SECONDARY))
                if include_unit.value and base_unit_name:
                    summary_rows.append(ft.Text(f"الوحدة: {base_unit_name}", size=11, color=Colors.TEXT_SECONDARY))
                body = ft.Column(
                    [
                        ft.Container(
                            ft.Column(summary_rows, spacing=3),
                            padding=12, bgcolor=Colors.PRIMARY_BG, border_radius=Radius.MD,
                            border=ft.border.all(1, Colors.PRIMARY_BORDER),
                        ),
                        ft.Text(
                            "المسح على جهاز آخر لنفس منشأتك يضيفها فورًا كموثوقة. من جهاز خارجي، تظهر كبيانات غير موثّقة تحتاج مراجعة قبل الحفظ.",
                            size=10, color=Colors.TEXT_FAINT,
                        ),
                    ],
                    spacing=10, tight=True,
                )
                footer = [
                    ft.TextButton("رجوع", icon=ft.Icons.ARROW_FORWARD_ROUNDED, on_click=lambda _: nav.back()),
                    ft.OutlinedButton("طباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=do_print),
                    ft.FilledButton("مشاركة PDF", icon=ft.Icons.IOS_SHARE_ROUNDED, on_click=do_pdf),
                ]
                return "الخطوة ٢ من ٢ — جاهزة للمشاركة", body, footer

            _open_paged_sheet(
                page, icon=ft.Icons.QR_CODE_2_ROUNDED, icon_color=Colors.PRIMARY, icon_bg=Colors.PRIMARY_BG,
                title="بطاقة المادة", page_count=2, render_page=render_page,
            )

        def open_import_card_sheet(_=None) -> None:
            """Scan a signed item card issued by open_item_card_sheet() (or
            an external one) and add/update the matching item -- a
            scan-then-confirm two-page flow instead of one screen mixing
            camera control and a data-entry form."""
            state = {"parsed": None, "status": None, "match": None, "nav": None, "merge_mode": "update"}
            scan_status = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)

            async def do_scan(_=None):
                if self.native_files is None:
                    scan_status.value = "مسح الباركود غير مهيأ في هذا البناء"
                    scan_status.color = Colors.DANGER
                    page.update()
                    return
                scan_status.value = "جارٍ فتح الكاميرا..."
                scan_status.color = Colors.TEXT_SECONDARY
                page.update()
                try:
                    code = await self.native_files.scan_barcode()
                except Exception as exc:
                    scan_status.value = str(exc); scan_status.color = Colors.DANGER; page.update()
                    return
                if not code:
                    scan_status.value = "لم تتم قراءة أي رمز (تم الإلغاء أو رفض إذن الكاميرا)"
                    scan_status.color = Colors.DANGER
                    page.update()
                    return
                status, parsed, reason = item_card_signing.verify_item_card(ctx.db, code)
                if status == "invalid":
                    scan_status.value = reason
                    scan_status.color = Colors.DANGER
                    scan_status.update()
                    return
                state["status"] = status
                state["parsed"] = parsed
                name = parsed.get("name") or ""
                barcode = parsed.get("barcode")
                state["match"] = None
                if barcode:
                    exact = next((i for i in ctx.items.list() if (i.get("barcode") or "") == barcode), None)
                    if exact:
                        state["match"] = exact
                if state["match"] is None and name:
                    similar = ctx.items.find_similar_names(name)
                    if similar:
                        found = next((i for i in ctx.items.list() if i.get("name") == similar[0]), None)
                        state["match"] = found
                state["merge_mode"] = "update" if state["match"] else "new"
                state["nav"].next()  # advance to the confirm page

            def do_save(nav) -> None:
                parsed = state["parsed"]
                if not parsed:
                    return
                cat_id = None
                if parsed.get("category"):
                    found_cat = next((c for c in categories if c["name"] == parsed["category"]), None)
                    cat_id = found_cat["id"] if found_cat else None
                unit_id = None
                if parsed.get("unit"):
                    found_unit = next((u for u in units if u["name"] == parsed["unit"]), None)
                    unit_id = found_unit["id"] if found_unit else None
                unit_id = unit_id or ensure_default_unit_id()
                kwargs = dict(
                    name=parsed.get("name") or "",
                    item_type="مخزون",
                    category_id=cat_id,
                    purchase_price=parsed.get("purchase_price") or 0,
                    selling_price=parsed.get("selling_price") or 0,
                    base_unit_id=int(unit_id) if unit_id else None,
                    item_units=[],
                    barcode=parsed.get("barcode"),
                )
                try:
                    match = state["match"] if state["merge_mode"] == "update" else None
                    if match:
                        ctx.items.update(int(match["id"]), **kwargs)
                        notify("تم تحديث المادة الموجودة")
                    else:
                        ctx.items.create(quantity=0, **kwargs)
                        notify("تمت إضافة المادة من البطاقة")
                    nav.close()
                    refresh()
                except Exception as exc:
                    notify(str(exc), kind="error")

            def render_page(idx: int, nav):
                state["nav"] = nav
                if idx == 0:
                    body = ft.Column(
                        [
                            ft.Text("وجّه الكاميرا نحو بطاقة QR لمادة صادرة من نانو (من هذا الجهاز أو من جهاز آخر).", size=12, color=Colors.TEXT_SECONDARY),
                            ft.FilledButton("فتح الكاميرا ومسح البطاقة", icon=ft.Icons.QR_CODE_SCANNER, on_click=do_scan, visible=supports("barcode_camera_scan")),
                            scan_status,
                        ],
                        spacing=12, tight=True,
                    )
                    footer = [ft.TextButton("إلغاء", on_click=lambda _: nav.close())]
                    return "الخطوة ١ من ٢ — المسح", body, footer

                parsed = state["parsed"] or {}
                status = state["status"]
                badge_map = {
                    "trusted": ("موقّعة ومطابقة — من هذا الجهاز أو من نفس منشأتك", Colors.SUCCESS_DARK, Colors.SUCCESS_BG),
                    "external": ("من مصدر خارجي — راجع البيانات قبل الحفظ", Colors.WARNING_DARK, Colors.WARNING_BG),
                }
                badge_text, badge_color, badge_bg = badge_map.get(status, ("", Colors.TEXT_SECONDARY, Colors.BACKGROUND_ALT))
                rows = [ft.Text(parsed.get("name") or "", weight=ft.FontWeight.BOLD)]
                if parsed.get("barcode"):
                    rows.append(ft.Text(f"الباركود: {parsed['barcode']}", size=11, color=Colors.TEXT_SECONDARY))
                if parsed.get("selling_price") is not None:
                    rows.append(
                        ft.Row(
                            [
                                ft.Text("سعر البيع: ", size=11, color=Colors.TEXT_SECONDARY),
                                ft.Text(self.money(parsed["selling_price"]), size=11, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                            ],
                            spacing=0, tight=True,
                        )
                    )
                if parsed.get("purchase_price") is not None:
                    rows.append(
                        ft.Row(
                            [
                                ft.Text("سعر الشراء: ", size=11, color=Colors.TEXT_SECONDARY),
                                ft.Text(self.money(parsed["purchase_price"]), size=11, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                            ],
                            spacing=0, tight=True,
                        )
                    )
                if parsed.get("category"):
                    rows.append(ft.Text(f"التصنيف: {parsed['category']}", size=11, color=Colors.TEXT_SECONDARY))
                if parsed.get("unit"):
                    rows.append(ft.Text(f"الوحدة: {parsed['unit']}", size=11, color=Colors.TEXT_SECONDARY))

                extra: list[ft.Control] = []
                if state["match"]:
                    def on_merge_mode_change(value):
                        state["merge_mode"] = value
                    merge_toggle = SegmentedToggle(
                        options=[
                            ("update", f"تحديث «{state['match'].get('name')}»"),
                            ("new", "مادة جديدة منفصلة"),
                        ],
                        value=state["merge_mode"],
                        on_change=lambda _: on_merge_mode_change(merge_toggle.value),
                    )
                    extra = [
                        ft.Text("توجد مادة مطابقة أو مشابهة بالفعل بهذا الجهاز:", size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY),
                        merge_toggle,
                    ]

                body = ft.Column(
                    [
                        ft.Container(
                            ft.Text(badge_text, size=11, weight=ft.FontWeight.W_600, color=badge_color),
                            padding=ft.padding.symmetric(horizontal=10, vertical=6), bgcolor=badge_bg, border_radius=20,
                        ),
                        ft.Container(ft.Column(rows, spacing=3), padding=12, bgcolor=Colors.BACKGROUND_ALT, border_radius=Radius.MD),
                        *extra,
                    ],
                    spacing=10, tight=True,
                )
                footer = [
                    ft.TextButton("رجوع للمسح", icon=ft.Icons.ARROW_FORWARD_ROUNDED, on_click=lambda _: nav.goto(0)),
                    ft.FilledButton("حفظ", icon=ft.Icons.CHECK_ROUNDED, on_click=lambda _: do_save(nav)),
                ]
                return "الخطوة ٢ من ٢ — المعاينة والحفظ", body, footer

            _open_paged_sheet(
                page, icon=ft.Icons.QR_CODE_SCANNER, icon_color=Colors.PURPLE, icon_bg=Colors.PURPLE_BG,
                title="استيراد بطاقة مادة", page_count=2, render_page=render_page,
            )

        def open_bulk_barcode_sheet(_=None) -> None:
            all_items = ctx.items.list()
            with_barcode = [i for i in all_items if str(i.get("barcode") or "").strip()]
            if not with_barcode:
                notify("لا توجد مواد لها باركود بعد — أضف باركودًا من نافذة تعديل المادة أولًا")
                return
            copies_fields: dict[int, ft.TextField] = {}
            checkboxes: dict[int, ft.Checkbox] = {}
            row_wrappers: dict[int, ft.Container] = {}
            # Nothing pre-selected on entry: printing labels for every item
            # in the catalog by default is rarely what's wanted, and it's
            # too easy to hit "متابعة" without noticing every row was
            # already ticked. The user picks what they actually want to
            # print, with "تحديد الكل" available for the (now opt-in) case
            # where that really is everything.
            selected: dict[int, bool] = {int(i["id"]): False for i in with_barcode}
            sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)

            # Printer profile chosen here travels with the selection into
            # open_barcode_print_dialog() below -- the admin's saved default
            # (core.barcode_settings) just seeds the initial chip state, it
            # doesn't lock the choice for this one print run.
            printer_layout = SegmentedToggle(
                options=[SegmentOption(k, v) for k, v in barcode_settings.LABEL_LAYOUT_LABELS.items()],
                value=barcode_settings.label_layout(ctx.settings),
            )
            roll_width_toggle = SegmentedToggle(
                options=[SegmentOption(k, v) for k, v in barcode_settings.LABEL_ROLL_WIDTH_LABELS.items()],
                value=barcode_settings.label_roll_width(ctx.settings),
            )
            roll_width_row = ft.Row([ft.Text("عرض اللفة:", size=11, color=Colors.TEXT_SECONDARY), roll_width_toggle], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, visible=printer_layout.value == "roll")

            def on_layout_change(_=None) -> None:
                roll_width_row.visible = printer_layout.value == "roll"
                roll_width_row.update()

            printer_layout.on_change = on_layout_change

            def close(_=None):
                page.close(sheet)

            def update_summary():
                n = sum(1 for v in selected.values() if v)
                counter_text.value = f"{n} من {len(with_barcode)} محدّدة للطباعة" if n else "لم يتم تحديد أي مادة بعد"
                counter_badge.bgcolor = Colors.PRIMARY_BG if n else Colors.BACKGROUND_ALT
                counter_badge.border = ft.border.all(1, Colors.PRIMARY_BORDER if n else Colors.BORDER)
                confirm_button.disabled = n == 0
                confirm_button.text = f"متابعة ({n})" if n else "متابعة"
                select_all_box.value = n == len(with_barcode)
                counter_text.update()
                counter_badge.update()
                confirm_button.update()
                select_all_box.update()

            def toggle(item_id: int, value: bool):
                selected[item_id] = value
                update_summary()

            def toggle_all(e):
                value = bool(e.control.value)
                for item_id, cb in checkboxes.items():
                    if not row_wrappers[item_id].visible:
                        continue  # search-filtered out: leave its state alone
                    selected[item_id] = value
                    cb.value = value
                    cb.update()
                update_summary()

            def confirm(_=None):
                entries = []
                for it in with_barcode:
                    item_id = int(it["id"])
                    if not selected.get(item_id):
                        continue
                    field = copies_fields.get(item_id)
                    try:
                        copies = int(float(field.value or 1))
                    except Exception:
                        copies = 1
                    entries.append((it, copies))
                if not entries:
                    notify("اختر مادة واحدة على الأقل للطباعة")
                    return
                layout = printer_layout.value or barcode_settings.DEFAULT_LABEL_LAYOUT
                roll_width_mm = barcode_settings.LABEL_ROLL_WIDTH_MM.get(roll_width_toggle.value or "", barcode_settings.label_roll_width_mm(ctx.settings))
                close()
                open_barcode_print_dialog(entries, layout=layout, roll_width_mm=roll_width_mm)

            def apply_search(e=None):
                term = (search_field.value or "").strip().lower()
                for it in with_barcode:
                    item_id = int(it["id"])
                    hay = f"{it.get('name', '')} {it.get('barcode', '')}".lower()
                    row_wrappers[item_id].visible = term in hay
                list_column.update()

            rows_ctrl = []
            for it in with_barcode:
                item_id = int(it["id"])
                copies_field = SelectAllTextField(value="1", width=56, text_align=ft.TextAlign.CENTER, keyboard_type=ft.KeyboardType.NUMBER, dense=True)
                copies_fields[item_id] = copies_field
                cb = ft.Checkbox(value=False, on_change=lambda e, i=item_id: toggle(i, bool(e.control.value)))
                checkboxes[item_id] = cb
                row = ft.Container(
                    ft.Row(
                        [
                            cb,
                            ft.Column([ft.Text(it["name"], size=12, weight=ft.FontWeight.W_600), ft.Text(str(it.get("barcode")), size=9, color=Colors.TEXT_SECONDARY, font_family="monospace")], expand=True, spacing=1),
                            ft.Column([ft.Text("نسخ", size=9, color=Colors.TEXT_FAINT), copies_field], spacing=1, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    bgcolor=Colors.WHITE, border=ft.border.all(1, Colors.BORDER), border_radius=14, shadow=Shadow.SM,
                    visible=True,
                )
                row_wrappers[item_id] = row
                rows_ctrl.append(row)

            search_field = SelectAllTextField(
                hint_text="ابحث بالاسم أو الباركود…", prefix_icon=ft.Icons.SEARCH, dense=True,
                border_radius=20, content_padding=ft.padding.symmetric(horizontal=14, vertical=8),
                border_color=Colors.BORDER, on_change=apply_search,
            )
            select_all_box = ft.Checkbox(value=False, label="تحديد الكل", on_change=toggle_all)
            counter_text = ft.Text("لم يتم تحديد أي مادة بعد", size=11, color=Colors.TEXT_SECONDARY)
            counter_badge = ft.Container(
                counter_text, padding=ft.padding.symmetric(horizontal=10, vertical=6),
                bgcolor=Colors.BACKGROUND_ALT, border=ft.border.all(1, Colors.BORDER), border_radius=20,
            )
            confirm_button = ft.FilledButton("متابعة", icon=ft.Icons.QR_CODE_2, on_click=confirm, expand=True, disabled=True)
            list_column = ft.Column(rows_ctrl, spacing=8, scroll=ft.ScrollMode.AUTO)

            printer_section = ft.Container(
                ft.Column(
                    [
                        ft.Row([ft.Icon(ft.Icons.PRINT_OUTLINED, size=15, color=Colors.TEXT_SECONDARY), ft.Text("نوع الطابعة", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_MUTED_DARK)], spacing=6),
                        printer_layout,
                        roll_width_row,
                    ],
                    spacing=8,
                ),
                padding=12, bgcolor=Colors.BACKGROUND_ALT, border_radius=Radius.MD,
            )

            sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Row(
                            [
                                _icon_bubble(ft.Icons.QR_CODE_2_ROUNDED, color=Colors.PURPLE, bgcolor=Colors.PURPLE_BG),
                                ft.Column(
                                    [
                                        ft.Text("طباعة ملصقات الباركود", size=17, weight=ft.FontWeight.BOLD),
                                        ft.Text("اختر المواد وعدد النسخ لكل ملصق ثم نوع الطابعة", size=11, color=Colors.TEXT_SECONDARY),
                                    ],
                                    spacing=1, expand=True,
                                ),
                                header_close_button(close),
                            ],
                            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        printer_section,
                        search_field,
                        ft.Row([select_all_box, ft.Container(expand=True), counter_badge], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        # This inner list keeps its own bounded height and
                        # scroll (same as the categories/units list in
                        # definitions_sheet) since it's a genuinely long
                        # repeating list, not a form -- the fix below is
                        # about letting the whole SHEET rise above the
                        # keyboard, not about how tall this particular list
                        # is.
                        ft.Container(list_column, height=300),
                        ft.Row(
                            [
                                ft.OutlinedButton("إلغاء", on_click=close, expand=True),
                                confirm_button,
                            ],
                            spacing=10,
                        ),
                    ],
                    # tight + scroll instead of a fixed pixel height on the
                    # outer Container (see the note in
                    # components/form_sheet.py for the full reasoning):
                    # combined with is_scroll_controlled +
                    # maintain_bottom_view_insets_padding on the sheet
                    # itself above, this lets the sheet grow to whatever
                    # height it needs and rise above the on-screen keyboard
                    # on its own, with Flutter auto-scrolling whichever
                    # "نسخ" field the user taps into view -- no manual
                    # height math to keep in sync as this sheet's content
                    # changes.
                    spacing=12, tight=True, scroll=ft.ScrollMode.AUTO,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=ft.padding.only(left=18, right=18, top=12, bottom=20),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=28, top_right=28),
                shadow=Shadow.LG,
            )
            page.open(sheet)

        def _bulk_apply_update(item_id: int, **overrides) -> None:
            """Full-record update helper for the bulk sheet below.

            ItemRepository.update() takes every field at once (no partial
            update) -- exactly what open_item_editor()'s perform_save()
            sends from open form fields for a single item. This reloads an
            item's current stored values instead and reapplies them
            unchanged except for whatever ``overrides`` actually asks to
            change, so a bulk category move never accidentally clears an
            item's alternate unit or barcode.
            """
            data = ctx.items.get(item_id)
            if not data:
                return
            current_units = ctx.items.units(item_id)
            alternate_units = [
                {"unit_id": int(u["id"]), "conversion_factor": float(u.get("conversion_factor") or 1), "selling_price": u.get("selling_price"), "purchase_price": u.get("purchase_price")}
                for u in current_units if not u.get("is_base")
            ]
            kwargs = dict(
                name=data["name"], item_type=data["item_type"],
                category_id=data.get("category_id"),
                purchase_price=data.get("purchase_price") or 0,
                selling_price=data.get("selling_price") or 0,
                base_unit_id=data.get("base_unit_id"),
                item_units=alternate_units,
                barcode=data.get("barcode"),
            )
            kwargs.update(overrides)
            ctx.items.update(item_id, **kwargs)

        def open_bulk_edit_sheet(_=None) -> None:
            all_items = ctx.items.list()
            if not all_items:
                notify("لا توجد مواد بعد")
                return
            selected: dict[int, bool] = {int(i["id"]): False for i in all_items}
            row_wrappers: dict[int, ft.Container] = {}
            checkboxes: dict[int, ft.Checkbox] = {}
            sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)

            def close(_=None):
                page.close(sheet)

            def update_summary():
                n = sum(1 for v in selected.values() if v)
                counter_text.value = f"{n} من {len(all_items)} محدّدة" if n else "لم يتم تحديد أي مادة"
                counter_badge.bgcolor = Colors.PRIMARY_BG if n else Colors.BACKGROUND_ALT
                counter_badge.border = ft.border.all(1, Colors.PRIMARY_BORDER if n else Colors.BORDER)
                move_button.disabled = n == 0 or not move_category.value
                price_button.disabled = n == 0
                select_all_box.value = n == len(all_items)
                counter_text.update(); counter_badge.update(); move_button.update(); price_button.update(); select_all_box.update()

            def toggle(item_id: int, value: bool):
                selected[item_id] = value
                update_summary()

            def toggle_all(e):
                value = bool(e.control.value)
                for item_id, cb in checkboxes.items():
                    if not row_wrappers[item_id].visible:
                        continue  # search-filtered out: leave its state alone
                    selected[item_id] = value
                    cb.value = value
                    cb.update()
                update_summary()

            def apply_search(e=None):
                term = (bulk_search.value or "").strip().lower()
                for it in all_items:
                    item_id = int(it["id"])
                    hay = f"{it.get('name', '')} {it.get('barcode', '')}".lower()
                    row_wrappers[item_id].visible = term in hay
                list_column.update()

            def selected_ids() -> list[int]:
                return [i for i, v in selected.items() if v]

            def apply_category_move(_=None):
                ids = selected_ids()
                if not ids or not move_category.value:
                    return
                new_cat_id = int(move_category.value)
                try:
                    for item_id in ids:
                        _bulk_apply_update(item_id, category_id=new_cat_id)
                except Exception as exc:
                    notify(str(exc), kind="error")
                    return
                notify(f"تم نقل {len(ids)} مادة إلى التصنيف الجديد")
                close()
                refresh()

            def apply_price_change(_=None):
                ids = selected_ids()
                if not ids:
                    return
                try:
                    pct = float(price_pct.value or 0)
                except ValueError:
                    notify("النسبة يجب أن تكون رقمًا")
                    return
                if pct == 0:
                    notify("أدخل نسبة تغيير غير صفرية")
                    return
                sign = 1 if price_mode["value"] == "up" else -1
                try:
                    for item_id in ids:
                        data = ctx.items.get(item_id)
                        if not data:
                            continue
                        old_price = float(data.get("selling_price") or 0)
                        new_price = max(0.0, old_price * (1 + sign * pct / 100))
                        _bulk_apply_update(item_id, selling_price=round(new_price, 2))
                except Exception as exc:
                    notify(str(exc), kind="error")
                    return
                notify(f"تم تعديل سعر البيع لـ {len(ids)} مادة")
                close()
                refresh()

            rows_ctrl = []
            for it in all_items:
                item_id = int(it["id"])
                cb = ft.Checkbox(value=False, on_change=lambda e, i=item_id: toggle(i, bool(e.control.value)))
                checkboxes[item_id] = cb
                row = ft.Container(
                    ft.Row(
                        [
                            cb,
                            ft.Column([ft.Text(it["name"], size=12, weight=ft.FontWeight.W_600), ft.Text(it.get("category_name") or "بلا تصنيف", size=9, color=Colors.TEXT_SECONDARY)], expand=True, spacing=1),
                            ft.Text(money(it.get("selling_price")), size=12, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    bgcolor=Colors.WHITE, border=ft.border.all(1, Colors.BORDER), border_radius=14, shadow=Shadow.SM,
                    visible=True,
                )
                row_wrappers[item_id] = row
                rows_ctrl.append(row)

            bulk_search = SelectAllTextField(
                hint_text="ابحث بالاسم أو الباركود…", prefix_icon=ft.Icons.SEARCH, dense=True,
                border_radius=20, content_padding=ft.padding.symmetric(horizontal=14, vertical=8),
                border_color=Colors.BORDER, on_change=apply_search,
            )
            select_all_box = ft.Checkbox(value=False, label="تحديد الكل", on_change=toggle_all)
            counter_text = ft.Text("لم يتم تحديد أي مادة", size=11, color=Colors.TEXT_SECONDARY)
            counter_badge = ft.Container(
                counter_text, padding=ft.padding.symmetric(horizontal=10, vertical=6),
                bgcolor=Colors.BACKGROUND_ALT, border=ft.border.all(1, Colors.BORDER), border_radius=20,
            )
            list_column = ft.Column(rows_ctrl, spacing=8, scroll=ft.ScrollMode.AUTO)

            # Real dropdown instead of the search-style field: it showed no
            # options until the user typed something and its expanding
            # results box left a large empty gap in the sheet once focused.
            # A native ft.Dropdown lists every category immediately on tap,
            # matching "ترتيب حسب" on this same screen and "نوع السند" on
            # the finance screen.
            move_category = ft.Dropdown(
                label="نقل التصنيف إلى",
                options=[ft.dropdown.Option(key=str(c["id"]), text=c["name"]) for c in categories],
                filled=True,
                bgcolor=Colors.BACKGROUND_ALT,
                border_radius=Radius.MD,
                border_color=Colors.BORDER,
                expand=2,
            )
            move_category.on_change = lambda _=None: update_summary()
            move_button = ft.FilledButton("نقل المحدد", icon=ft.Icons.DRIVE_FILE_MOVE_OUTLINED, on_click=apply_category_move, disabled=True, expand=1)

            # Percentage only (not a fixed amount): a flat +500 makes sense
            # for one price scale and is meaningless for another, while a
            # percentage bump ("+10%" from a supplier) applies correctly
            # regardless of the item's current price. Purchase price/cost
            # is deliberately left untouched here -- that feeds average-cost
            # COGS accounting elsewhere, so it stays a one-item-at-a-time
            # edit in the regular item editor rather than a bulk action.
            price_mode = {"value": "up"}
            price_pct = SelectAllTextField(label="النسبة %", value="10", keyboard_type=ft.KeyboardType.NUMBER, width=90)
            up_chip = ft.Container(ft.Text("رفع", size=12, weight=ft.FontWeight.W_600), padding=ft.padding.symmetric(horizontal=14, vertical=9), border_radius=12, ink=True)
            down_chip = ft.Container(ft.Text("خفض", size=12, weight=ft.FontWeight.W_600), padding=ft.padding.symmetric(horizontal=14, vertical=9), border_radius=12, ink=True)

            def style_price_mode(do_update: bool = True):
                up_chip.bgcolor = Colors.PRIMARY if price_mode["value"] == "up" else Colors.WHITE
                up_chip.content.color = Colors.WHITE if price_mode["value"] == "up" else Colors.TEXT_MUTED
                up_chip.border = ft.border.all(1, Colors.PRIMARY if price_mode["value"] == "up" else Colors.BORDER)
                down_chip.bgcolor = Colors.DANGER if price_mode["value"] == "down" else Colors.WHITE
                down_chip.content.color = Colors.WHITE if price_mode["value"] == "down" else Colors.TEXT_MUTED
                down_chip.border = ft.border.all(1, Colors.DANGER if price_mode["value"] == "down" else Colors.BORDER)
                # Only call update() once these chips are actually on the
                # page (i.e. after page.open(sheet) below) -- calling
                # update() on a freshly-created control that isn't attached
                # to the page tree yet raises in Flet, which was silently
                # aborting this whole function before it ever reached
                # page.open(sheet), so the sheet never opened.
                if do_update:
                    up_chip.update(); down_chip.update()

            def set_price_mode(mode: str):
                price_mode["value"] = mode
                style_price_mode()

            up_chip.on_click = lambda _: set_price_mode("up")
            down_chip.on_click = lambda _: set_price_mode("down")
            style_price_mode(do_update=False)
            price_button = ft.FilledButton("تطبيق على سعر البيع", icon=ft.Icons.PERCENT, on_click=apply_price_change, disabled=True, expand=True)

            def _action_card(icon: str, title: str, controls: list[ft.Control]) -> ft.Container:
                return ft.Container(
                    ft.Column(
                        [
                            ft.Row([ft.Icon(icon, size=15, color=Colors.PRIMARY), ft.Text(title, size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_MUTED_DARK)], spacing=6),
                            *controls,
                        ],
                        spacing=10,
                    ),
                    padding=12, bgcolor=Colors.BACKGROUND_ALT, border_radius=Radius.MD,
                )

            move_card = _action_card(
                ft.Icons.DRIVE_FILE_MOVE_OUTLINED, "نقل إلى تصنيف",
                [ft.Row([move_category, move_button], spacing=8, vertical_alignment=ft.CrossAxisAlignment.END)],
            )
            price_card = _action_card(
                ft.Icons.PERCENT, "تعديل نسبة سعر البيع",
                [ft.Row([up_chip, down_chip, price_pct, price_button], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)],
            )

            sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Row(
                            [
                                _icon_bubble(ft.Icons.EDIT_NOTE_OUTLINED, color=Colors.PRIMARY, bgcolor=Colors.PRIMARY_BG),
                                ft.Column(
                                    [
                                        ft.Text("تعديل جماعي", size=17, weight=ft.FontWeight.BOLD),
                                        ft.Text("اختر مواد ثم طبّق نقل تصنيف أو تعديل سعر بيع على المحدد دفعة واحدة", size=11, color=Colors.TEXT_SECONDARY),
                                    ],
                                    spacing=1, expand=True,
                                ),
                                header_close_button(close),
                            ],
                            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bulk_search,
                        ft.Row([select_all_box, ft.Container(expand=True), counter_badge], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.Container(list_column, height=240),
                        ft.Divider(height=1, color=Colors.BORDER),
                        move_card,
                        price_card,
                    ],
                    spacing=12, tight=True, scroll=ft.ScrollMode.AUTO,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=ft.padding.only(left=18, right=18, top=12, bottom=20),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=28, top_right=28),
                shadow=Shadow.LG,
            )
            page.open(sheet)

        def open_bulk_add_sheet(_=None) -> None:
            """Comprehensive multi-item entry: a full table in a wide sheet.

            Every column is editable per row (name, barcode, category, unit,
            type, prices, quantity) and the grid carries the table toolkit:
            column visibility, density, sorting, multi-select with batch
            duplicate/delete, per-row menu, CSV export and live footer
            totals. The selectors above the grid act as defaults stamped
            onto each new row ("تطبيق على الكل" stamps the whole sheet).
            Saving is one batch; on a partial failure the rows that did save
            are removed from the grid so a retry never re-creates them.
            """
            cat_options = [(str(c["id"]), c["name"]) for c in categories]
            default_unit = ensure_default_unit_id()
            unit_options = [(str(u["id"]), u["name"]) for u in units]
            cat_map = {str(c["id"]): c["name"] for c in categories}
            unit_map = {str(u["id"]): u["name"] for u in units}

            bulk_hidden_state = {"keys": set(filter(None, ctx.settings.get("ui.bulk_add_hidden_columns", "").split(",")))}
            bulk_density_state = {"compact": ctx.settings.get("ui.bulk_add_density", "comfortable") == "compact"}

            type_sel = SearchSelect(
                label="النوع الافتراضي", choices=[("مخزون", "مخزون"), ("خدمة", "خدمة")],
                value="مخزون", allow_clear=False,
            )
            cat_sel = SearchSelect(label="التصنيف الافتراضي", choices=cat_options, value=None)
            unit_sel = SearchSelect(
                label="الوحدة الافتراضية", choices=unit_options,
                value=default_unit, allow_clear=False,
            )

            bulk_columns = [
                GridColumn("name", "الاسم", editable=True, tooltip="اسم المادة أو الخدمة"),
                GridColumn("barcode", "الباركود", width=150, editable=True, tooltip="الباركود الأساسي (اختياري)"),
                GridColumn("category_id", "التصنيف", width=140, editable=True, kind="picker", tooltip="نقرة على الخلية ثم Enter للاختيار"),
                GridColumn("item_type", "النوع", width=100, align="center", editable=True, kind="picker", tooltip="مخزون يُتابع بالمخزون، وخدمة لا"),
                GridColumn("base_unit_id", "الوحدة", width=110, editable=True, kind="picker", tooltip="الوحدة الأساسية للبيع والشراء"),
                GridColumn("quantity", "الكمية", width=90, align="end", kind="number", editable=True, tooltip="الكمية الافتتاحية (للمخزون فقط)"),
                GridColumn("purchase_price", "سعر الشراء", width=120, align="end", kind="money", editable=True),
                GridColumn("selling_price", "سعر البيع", width=120, align="end", kind="money", editable=True),
                GridColumn("alternate_unit_id", "الوحدة الفرعية", width=120, editable=True, kind="picker", tooltip="وحدة فرعية اختيارية"),
                GridColumn("alternate_conversion_factor", "معامل التحويل", width=110, align="end", kind="number", editable=True, tooltip="عدد الوحدات الأساسية في الوحدة الفرعية"),
                GridColumn("alternate_purchase_price", "شراء الوحدة الفرعية", width=140, align="end", kind="money", editable=True),
                GridColumn("alternate_selling_price", "بيع الوحدة الفرعية", width=140, align="end", kind="money", editable=True),
                GridColumn("alternate_barcode", "باركود فرعي", width=150, editable=True, tooltip="باركود اختياري للوحدة الفرعية"),
                GridColumn("_status", "الحالة", width=110, align="center", sortable=False, tooltip="جاهز للحفظ / ناقص الاسم"),
            ]

            def bulk_formatter(col: GridColumn, value, row: dict):
                if col.key == "category_id":
                    return cat_map.get(str(value)) if value else "بلا تصنيف"
                if col.key == "base_unit_id":
                    return unit_map.get(str(value)) if value else None
                if col.key == "alternate_unit_id":
                    return unit_map.get(str(value)) if value else None
                if col.key == "item_type":
                    return value or "مخزون"
                if col.key == "_status":
                    if str(row.get("name") or "").strip():
                        return ("جاهز", Colors.SUCCESS_DARK, Colors.SUCCESS_BG)
                    return ("ناقص الاسم", Colors.TEXT_FAINT, Colors.BACKGROUND_ALT)
                if col.kind == "money":
                    return money(value) if value else ""
                return None

            def bulk_row_bg(row: dict) -> str | None:
                """Flag rows whose barcode duplicates another row's in the draft."""
                code = str(row.get("barcode") or "").strip()
                if not code:
                    return None
                copies = sum(1 for other in bulk_grid.rows if str(other.get("barcode") or "").strip() == code)
                return Colors.DANGER_BG if copies > 1 else None

            bulk_bar = ft.Container(
                visible=False,
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                border_radius=Radius.SM,
                bgcolor=Colors.PRIMARY_BG,
            )

            # The table needs its own bounded height to become a real
            # viewport inside the sheet. Leaving it to `expand` inside a
            # bottom-sheet scroll view let Flutter collapse it to zero
            # (the "grey empty block" the table used to render as); the
            # proven pattern from the invoice quick-entry sheet is an
            # explicit viewport height with the sheet body scrolling.
            grid_viewport = max(240, int(int(page.height or 800) * 0.85) - 330)
            bulk_grid = DataGrid(
                columns=bulk_columns, rows=[], page=page,
                viewport_height=grid_viewport,
                row_height=36 if bulk_density_state["compact"] else 48,
                hidden_keys=set(bulk_hidden_state["keys"]),
                formatter=bulk_formatter,
                edit_formatter=lambda col, value, row: _edit_item_cell_text(col, value, row, ctx.settings),
                parser=lambda col, text: _parse_item_cell(col, text, ctx.settings),
                row_bg=bulk_row_bg,
                on_cell_commit=lambda *_: refresh_bulk_footer(),
                picker_options=lambda col, _row: bulk_choices(col.key),
                on_sort=lambda key, direction: apply_bulk_sort(key, direction),
                on_row_menu=lambda row: bulk_row_menu(row),
                selectable=True,
                on_selection_change=lambda rows: render_bulk_selection(rows),
                empty_text="اضغط «سطر جديد» لبدء الإدخال",
            )

            def bulk_stats() -> dict:
                ready, qty_total, buy_total, sell_total = 0, 0.0, 0.0, 0.0
                for row in bulk_grid.rows:
                    if not str(row.get("name") or "").strip():
                        continue
                    ready += 1
                    if str(row.get("item_type") or "مخزون") == "مخزون":
                        qty_total += float(row.get("quantity") or 0)
                    buy_total += float(row.get("purchase_price") or 0)
                    sell_total += float(row.get("selling_price") or 0)
                return {"ready": ready, "total": len(bulk_grid.rows), "qty": qty_total, "buy": buy_total, "sell": sell_total}

            def refresh_bulk_footer() -> None:
                """Live summary: rows ready to save plus quantity/value totals."""
                stats = bulk_stats()
                if not bulk_grid.rows:
                    bulk_grid.set_footer(None)
                    return
                bulk_grid.set_footer({
                    "name": f"{stats['ready']} جاهز من {stats['total']} صف",
                    "quantity": qty_fmt(stats["qty"]) if stats["qty"] else "",
                    "purchase_price": money(stats["buy"]) if stats["buy"] else "",
                    "selling_price": money(stats["sell"]) if stats["sell"] else "",
                })

            def default_id(selector) -> int | None:
                try:
                    return int(selector.value) if selector.value else None
                except (TypeError, ValueError):
                    return None

            def row_defaults() -> dict:
                return {
                    "item_type": type_sel.value or "مخزون",
                    "category_id": default_id(cat_sel) or "",
                    "base_unit_id": default_id(unit_sel) or "",
                    "alternate_unit_id": "",
                    "alternate_conversion_factor": 1.0,
                    "alternate_purchase_price": "",
                    "alternate_selling_price": "",
                    "alternate_barcode": "",
                }

            def add_blank(_=None) -> None:
                bulk_grid.add_row(row_defaults())
                refresh_bulk_footer()

            def duplicate_rows(rows: list[dict]) -> None:
                for row in rows:
                    data = {k: v for k, v in row.items() if k != "_status"}
                    bulk_grid.add_row(data)
                if rows:
                    refresh_bulk_footer()

            def delete_row(target: dict) -> None:
                for index, row in enumerate(bulk_grid.rows):
                    if row == target:
                        bulk_grid.remove_row(index)
                        break
                refresh_bulk_footer()

            def bulk_row_menu(row: dict) -> list:
                return [
                    ("تكرار الصف", ft.Icons.CONTENT_COPY_OUTLINED, lambda r=row: duplicate_rows([dict(r)])),
                    ("حذف الصف", ft.Icons.DELETE_OUTLINE, lambda r=row: delete_row(r)),
                ]

            def remove_selected_rows() -> None:
                selected = bulk_grid.selected_rows()
                if not selected:
                    return
                rows_now = bulk_grid.rows
                doomed: list[int] = []
                for wanted in selected:
                    for index, row in enumerate(rows_now):
                        if index not in doomed and row == wanted:
                            doomed.append(index)
                            break
                for index in sorted(doomed, reverse=True):
                    bulk_grid.remove_row(index)
                refresh_bulk_footer()

            def render_bulk_selection(rows: list[dict]) -> None:
                if not rows:
                    bulk_bar.visible = False
                    bulk_bar.content = None
                else:
                    bulk_bar.content = ft.Row(
                        [
                            ft.Icon(ft.Icons.CHECK_BOX, size=16, color=Colors.PRIMARY),
                            ft.Text(f"تم تحديد {len(rows)} صف", size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK),
                            ft.OutlinedButton("تكرار المحدد", icon=ft.Icons.CONTENT_COPY_OUTLINED, on_click=lambda _=None: duplicate_rows(list(rows))),
                            ft.OutlinedButton(
                                "حذف المحدد", icon=ft.Icons.DELETE_OUTLINE,
                                style=ft.ButtonStyle(color=Colors.DANGER),
                                on_click=lambda _=None: remove_selected_rows(),
                            ),
                            ft.TextButton("إلغاء التحديد", on_click=lambda _=None: bulk_grid.clear_selection()),
                        ],
                        spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                    bulk_bar.visible = True
                try:
                    bulk_bar.update()
                except Exception:
                    pass

            def apply_bulk_sort(key, direction) -> None:
                if key:
                    def sort_value(row: dict):
                        value = row.get(key)
                        if key in (
                            "quantity", "purchase_price", "selling_price",
                            "alternate_conversion_factor", "alternate_purchase_price",
                            "alternate_selling_price",
                        ):
                            try:
                                return float(value or 0)
                            except (TypeError, ValueError):
                                return 0.0
                        return str(value or "")

                    bulk_grid.set_rows(sorted(bulk_grid.rows, key=sort_value, reverse=direction == "desc"))
                refresh_bulk_footer()

            def bulk_toggle_column(key: str) -> None:
                if key in bulk_hidden_state["keys"]:
                    bulk_hidden_state["keys"].discard(key)
                else:
                    bulk_hidden_state["keys"].add(key)
                ctx.settings.set("ui.bulk_add_hidden_columns", ",".join(sorted(bulk_hidden_state["keys"])))
                bulk_grid.set_hidden_keys(bulk_hidden_state["keys"])
                page.update()

            def bulk_toggle_density(_=None) -> None:
                compact = not bulk_density_state["compact"]
                bulk_density_state["compact"] = compact
                bulk_grid.set_row_height(36 if compact else 48)
                bulk_density_button.icon = ft.Icons.DENSITY_SMALL if compact else ft.Icons.DENSITY_MEDIUM
                bulk_density_button.tooltip = "عرض مريح" if compact else "عرض مضغوط"
                ctx.settings.set("ui.bulk_add_density", "compact" if compact else "comfortable")
                page.update()

            async def bulk_export_csv(_=None) -> None:
                rows = [row for row in bulk_grid.rows if str(row.get("name") or "").strip()]
                if not rows:
                    notify("لا توجد صفوف جاهزة للتصدير", kind="warning")
                    return
                if self.native_files is None:
                    notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                    return
                try:
                    path = Path(tempfile.gettempdir()) / f"nano_bulk_items_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                        writer = _csv.writer(fh)
                        writer.writerow([
                            "الاسم", "الباركود", "التصنيف", "النوع", "الوحدة", "الكمية",
                            "سعر الشراء", "سعر البيع", "الوحدة الفرعية", "معامل التحويل",
                            "شراء الوحدة الفرعية", "بيع الوحدة الفرعية", "باركود فرعي",
                        ])
                        for row in rows:
                            writer.writerow([
                                row.get("name") or "",
                                row.get("barcode") or "",
                                cat_map.get(str(row.get("category_id") or "")) or "",
                                row.get("item_type") or "مخزون",
                                unit_map.get(str(row.get("base_unit_id") or "")) or "",
                                row.get("quantity") or 0,
                                row.get("purchase_price") or 0,
                                row.get("selling_price") or 0,
                                unit_map.get(str(row.get("alternate_unit_id") or "")) or "",
                                row.get("alternate_conversion_factor") or 1,
                                row.get("alternate_purchase_price") or "",
                                row.get("alternate_selling_price") or "",
                                row.get("alternate_barcode") or "",
                            ])
                    await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير مسودة الإدخال")
                    notify("تم تجهيز ملف CSV للصفوف الجاهزة")
                except Exception as exc:
                    notify(str(exc), kind="error")

            def bulk_choices(key: str) -> list[tuple]:
                if key == "category_id":
                    return [(None, "بلا تصنيف")] + [(int(c["id"]), c["name"]) for c in categories]
                if key == "base_unit_id":
                    return [(int(u["id"]), u["name"]) for u in units]
                if key == "alternate_unit_id":
                    return [(None, "بلا وحدة فرعية")] + [(int(u["id"]), u["name"]) for u in units]
                if key == "item_type":
                    return [("مخزون", "مخزون"), ("خدمة", "خدمة")]
                return []

            def apply_defaults_to_all(_=None) -> None:
                defaults = row_defaults()
                for index, row in enumerate(bulk_grid.rows):
                    for key, value in defaults.items():
                        if str(row.get(key) or "") != str(value or ""):
                            bulk_grid.set_cell(index, key, value, notify=False)
                refresh_bulk_footer()
                notify("تم تطبيق الافتراضيات على كل الصفوف")

            def save_all(_=None) -> None:
                type_default = type_sel.value or "مخزون"
                created_indices: list[int] = []
                errors: list[str] = []
                warnings: list[str] = []
                for index, row in enumerate(bulk_grid.rows):
                    name = str(row.get("name") or "").strip()
                    if not name:
                        continue
                    try:
                        alternate_unit_id = int(row["alternate_unit_id"]) if row.get("alternate_unit_id") else None
                        alternate_units = []
                        if alternate_unit_id is not None:
                            alternate_units.append({
                                "unit_id": alternate_unit_id,
                                "conversion_factor": float(row.get("alternate_conversion_factor") or 1),
                                "purchase_price": row.get("alternate_purchase_price") or None,
                                "selling_price": row.get("alternate_selling_price") or None,
                            })
                        item_id = ctx.items.create(
                            name=name,
                            category_id=int(row["category_id"]) if row.get("category_id") else None,
                            item_type=str(row.get("item_type") or type_default),
                            purchase_price=row.get("purchase_price") or 0,
                            selling_price=row.get("selling_price") or 0,
                            quantity=row.get("quantity") or 0,
                            base_unit_id=int(row["base_unit_id"]) if row.get("base_unit_id") else None,
                            item_units=alternate_units,
                            barcode=str(row.get("barcode") or "").strip() or None,
                        )
                        created_indices.append(index)
                        alternate_barcode = str(row.get("alternate_barcode") or "").strip()
                        if alternate_barcode:
                            try:
                                ctx.items.add_barcode(
                                    item_id, alternate_barcode, unit_id=alternate_unit_id,
                                )
                            except Exception as exc:
                                warnings.append(f"{name} — باركود فرعي: {exc}")
                    except Exception as exc:
                        errors.append(f"{name}: {exc}")
                # Drop the rows that did save; a retry can then only ever
                # process the ones that failed (no duplicate-barcode loops).
                for index in sorted(created_indices, reverse=True):
                    bulk_grid.remove_row(index)
                refresh_bulk_footer()
                if not created_indices and not errors:
                    notify("لا توجد صفوف للحفظ", kind="error")
                    return
                if created_indices:
                    notify(f"تمت إضافة {len(created_indices)} مادة", sound_kind="success")
                if errors:
                    notify(errors[0] if len(errors) == 1 else f"تعذّر حفظ بعض البيانات: {errors[0]}", kind="error")
                    page.update()
                    return
                if warnings:
                    notify(warnings[0] if len(warnings) == 1 else f"تمت الإضافة مع تعذّر حفظ بعض الباركودات: {warnings[0]}", kind="warning")
                close()
                refresh()

            # While one of the header selectors has focus, its field eats
            # Backspace/Enter -- the grid must not also act on those keys
            # (same reason the list disarms on its search box). The grid
            # re-arms on the next cell click or when a row is added.
            def _disarm_on_focus(selector) -> None:
                previous = selector.field.on_focus

                def chained(e, _prev=previous):
                    bulk_grid.disarm()
                    if callable(_prev):
                        _prev(e)

                selector.field.on_focus = chained

            for _selector in (type_sel, cat_sel, unit_sel):
                _disarm_on_focus(_selector)

            bulk_grid.add_row(row_defaults())
            refresh_bulk_footer()

            # A wide, tall sheet: the table is the point, so it gets the
            # full workbench width instead of the narrow default column.
            page_w = int(page.width or 1280)
            page_h = int(page.height or 800)
            sheet_w = max(360, page_w - 32)
            sheet_h = max(420, int(page_h * 0.85))

            sheet = ft.BottomSheet(
                content=ft.Container(),
                is_scroll_controlled=True,
                enable_drag=True,
                maintain_bottom_view_insets_padding=True,
                size_constraints=ft.BoxConstraints(min_width=sheet_w, max_width=sheet_w),
            )
            # Drag-dismissing the sheet never runs close(); restore the list
            # grid's keyboard handler here too, or the list loses arrow/Enter
            # navigation until the next navigation event.
            sheet.on_dismiss = lambda e: grid.attach()

            def close(_=None):
                # The bulk grid took over the page's keyboard handler when it
                # was built; hand it back to the list grid on the way out.
                page.close(sheet)
                grid.attach()

            bulk_columns_button = ft.PopupMenuButton(
                icon=ft.Icons.VIEW_COLUMN,
                tooltip="إظهار/إخفاء الأعمدة",
                items=[
                    ft.PopupMenuItem(
                        text=col.label,
                        checked=col.key not in bulk_hidden_state["keys"],
                        on_click=lambda _, k=col.key: bulk_toggle_column(k),
                    )
                    for col in bulk_columns
                ],
            )
            bulk_density_button = ft.IconButton(
                icon=ft.Icons.DENSITY_SMALL if bulk_density_state["compact"] else ft.Icons.DENSITY_MEDIUM,
                tooltip="عرض مريح" if bulk_density_state["compact"] else "عرض مضغوط",
                on_click=bulk_toggle_density,
            )
            bulk_export_button = ft.IconButton(
                icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
                tooltip="تصدير الصفوف الجاهزة CSV",
                on_click=bulk_export_csv,
            )
            sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Row(
                            [
                                _icon_bubble(ft.Icons.VIEW_LIST_OUTLINED, color=Colors.PRIMARY, bgcolor=Colors.PRIMARY_BG),
                                ft.Column(
                                    [
                                        ft.Text("إضافة شاملة (جدول)", size=17, weight=ft.FontWeight.BOLD),
                                        ft.Text("كل الحقول في جدول متكامل: أعمدة قابلة للإظهار والترتيب والتصدير، وأسطر تُكرر أو تُحذف، ثم احفظ الكل دفعة واحدة", size=11, color=Colors.TEXT_SECONDARY),
                                    ],
                                    spacing=1, expand=True,
                                ),
                                header_close_button(close),
                            ],
                            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Row(
                            [
                                ft.Text("افتراضيات الصف الجديد", size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY),
                                ft.Container(type_sel, width=150),
                                ft.Container(cat_sel, width=210),
                                ft.Container(unit_sel, width=190),
                                ft.OutlinedButton("تطبيق على الكل", icon=ft.Icons.DONE_ALL_ROUNDED, on_click=apply_defaults_to_all),
                            ],
                            spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Row(
                            [
                                ft.FilledButton("سطر جديد", icon=ft.Icons.ADD, on_click=add_blank),
                                ft.OutlinedButton("تكرار الأخير", icon=ft.Icons.CONTENT_COPY_OUTLINED, on_click=lambda _=None: duplicate_rows(bulk_grid.rows[-1:] if bulk_grid.rows else [])),
                                ft.Text("Enter للتحرير • Tab للتنقل • Shift+الأسهم لتحديد نطاق • Ctrl+C/V نسخ/لصق • Ctrl+Z/Y تراجع/إعادة", size=10, color=Colors.TEXT_SECONDARY),
                                bulk_columns_button,
                                bulk_density_button,
                                bulk_export_button,
                            ],
                            spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bulk_bar,
                        ft.Container(bulk_grid, height=grid_viewport, border=ft.border.all(1, Colors.BORDER), border_radius=Radius.MD, clip_behavior=ft.ClipBehavior.HARD_EDGE),
                        ft.Row(
                            [
                                ft.Text("الحفظ يتجاهل الصفوف بلا اسم", size=10, color=Colors.TEXT_SECONDARY),
                                ft.Container(expand=True),
                                ft.FilledButton("حفظ الكل", icon=ft.Icons.SAVE_OUTLINED, on_click=save_all),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=10, tight=True, scroll=ft.ScrollMode.AUTO,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                width=sheet_w,
                height=sheet_h,
                padding=ft.padding.only(left=18, right=18, top=12, bottom=16),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=28, top_right=28),
                shadow=Shadow.LG,
                rtl=True,
            )
            page.open(sheet)

        def show_item_detail(item: dict):
            try:
                data = ctx.items.get(int(item["id"])) or item
                stats = ctx.items.activity_summary(int(item["id"]))
                movements = ctx.items.movements(int(item["id"]), limit=20)
                item_units = ctx.items.units(int(item["id"]))
            except Exception as exc:
                notify(str(exc), kind="error"); return
            move_cards = []
            type_labels = {"sale": "بيع", "purchase": "شراء", "adjustment": "تسوية"}
            for mv in movements:
                delta = float(mv.get("quantity_delta") or 0)
                move_cards.append(
                    ft.Container(
                        ft.Row([
                            ft.Container(ft.Icon(ft.Icons.ARROW_DOWNWARD if delta > 0 else ft.Icons.ARROW_UPWARD, size=15, color=Colors.SUCCESS if delta > 0 else Colors.DANGER), width=32, height=32, alignment=ft.alignment.center, bgcolor=Colors.BACKGROUND, border_radius=10),
                            ft.Column([ft.Text(type_labels.get(mv.get("movement_type"), str(mv.get("movement_type"))), size=11, weight=ft.FontWeight.BOLD), ft.Text(f"{mv.get('movement_date') or '—'} • فاتورة #{mv.get('invoice_id') or '—'}", size=9, color=Colors.TEXT_SECONDARY)], spacing=1, expand=True),
                            ft.Text(f"{delta:+,.2f}", size=12, weight=ft.FontWeight.BOLD, color=Colors.SUCCESS if delta > 0 else Colors.DANGER),
                        ]), padding=8, bgcolor=Colors.BACKGROUND, border_radius=11,
                    )
                )
            if not move_cards:
                move_cards = [ft.Text("لا توجد حركات مخزون بعد", size=11, color=Colors.TEXT_SECONDARY)]
            units_text = "، ".join(f"{u['name']} × {float(u.get('conversion_factor') or 1):g}" for u in item_units) or "بلا وحدة"

            # Deletion is only allowed while the item has no history -- the
            # repository would reject it anyway (see confirm_delete_item).
            # Derive the flag here because ctx.items.get() (used for `data`
            # below) does not carry the list query's has_activity column.
            can_delete = not (
                bool(item.get("has_activity"))
                or any(float(stats.get(k) or 0) for k in ("sold_qty", "purchased_qty", "sale_count", "purchase_count"))
                or bool(movements)
            )

            # Bottom sheet instead of a centered AlertDialog: this is a
            # read-mostly detail view (no destructive/confirm action), so a
            # sheet the user can drag/dismiss reads more natural on a phone
            # than a modal box floating mid-screen. Reuses the exact
            # BottomSheet + drag-handle + rounded-top pattern already
            # proven in main.py's "المزيد" sheet — not a new, untested one.
            detail_sheet = ft.BottomSheet(content=ft.Container(), enable_drag=True, maintain_bottom_view_insets_padding=True)

            def close(_=None): page.close(detail_sheet)

            async def edit(_=None):
                # Closing detail_sheet and opening the editor's BottomSheet
                # in the same synchronous tick races Flutter's bottom-sheet
                # dismiss animation: the editor sheet gets built and its
                # `open` flag set, but nothing renders because the previous
                # sheet's pop route hasn't finished. A short yield lets the
                # close animation complete first, so the editor actually
                # appears (same class of "invisible because still-open
                # overlay" issue as the SnackBar-behind-scrim case above).
                close()
                await asyncio.sleep(0.1)
                open_item_editor(data)

            async def delete_it(_=None):
                # The desktop grid has no swipe-to-delete, so deletion lives
                # here. Same close-then-act yield as edit(): the confirm
                # dialog must not open while the sheet's dismiss animation is
                # still running (its pop route would swallow the new overlay).
                close()
                await asyncio.sleep(0.1)
                confirm_delete_item(data)

            # Same fix as the item editor and barcode-print sheets above:
            # give the OUTER sheet Container an explicit height computed
            # from the real screen height instead of letting Flutter
            # shrink-wrap header + fixed-420px stats/history + footer and
            # clip whatever exceeds its ~9/16-screen ceiling. The
            # طباعة/تعديل/إغلاق row is pulled out of the scrollable content
            # into a fixed footer so it's always reachable.
            page_h = page.height or 780
            total_sheet_h = min(int(page_h * 0.55), 600)
            header_reserved, footer_reserved, gaps = 90, 100, 24
            body_area_height = max(180, total_sheet_h - header_reserved - footer_reserved - gaps)

            detail_sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Row([
                            ft.Container(ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, color=Colors.PRIMARY), width=42, height=42, alignment=ft.alignment.center, bgcolor=Colors.PRIMARY_BG, border_radius=14),
                            ft.Column([ft.Text(data["name"], size=18, weight=ft.FontWeight.BOLD), ft.Text(f"{data.get('item_type')} • {data.get('category_name') or 'بلا تصنيف'}", size=10, color=Colors.TEXT_SECONDARY)], spacing=1, expand=True),
                            ft.Column(
                                [
                                    money_row(data.get("selling_price"), ctx.settings, size=14, weight=ft.FontWeight.BOLD),
                                    ft.Text("سعر البيع", size=9, color=Colors.TEXT_SECONDARY),
                                ],
                                spacing=1,
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                            ),
                        ]),
                        ft.Container(
                            ft.Column([
                                metric_row([
                                    kpi_card("المخزون", qty_fmt(stats.get("quantity")), ft.Icons.INVENTORY, Colors.PRIMARY),
                                    kpi_card("متوسط التكلفة", "", ft.Icons.PRICE_CHECK_OUTLINED, Colors.PURPLE, amount_usd=stats.get("average_cost"), settings=ctx.settings),
                                    kpi_card("قيمة المخزون", "", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.SUCCESS, amount_usd=stats.get("inventory_cost_value"), settings=ctx.settings),
                                    kpi_card("بسعر البيع", "", ft.Icons.SELL_OUTLINED, Colors.WARNING, amount_usd=stats.get("inventory_sale_value"), settings=ctx.settings),
                                ], spacing=7),
                                metric_row([
                                    kpi_card("كمية مباعة", qty_fmt(stats.get("sold_qty")), ft.Icons.TRENDING_UP, Colors.DANGER),
                                    kpi_card("كمية مشتراة", qty_fmt(stats.get("purchased_qty")), ft.Icons.TRENDING_DOWN, Colors.SUCCESS),
                                    kpi_card("فواتير بيع", str(int(stats.get("sale_count") or 0)), ft.Icons.RECEIPT_LONG, Colors.PRIMARY),
                                    kpi_card("فواتير شراء", str(int(stats.get("purchase_count") or 0)), ft.Icons.SHOPPING_BAG_OUTLINED, Colors.PURPLE),
                                ], spacing=7),
                                ft.Text(f"الوحدات: {units_text}", size=10, color=Colors.TEXT_MUTED),
                                ft.Text(f"آخر بيع: {stats.get('last_sale_date') or '—'}   •   آخر شراء: {stats.get('last_purchase_date') or '—'}", size=10, color=Colors.TEXT_SECONDARY),
                                ft.Divider(height=8), ft.Text("آخر حركات المخزون", size=14, weight=ft.FontWeight.BOLD), ft.Column(move_cards, spacing=5),
                            ], spacing=9, scroll=ft.ScrollMode.AUTO),
                            height=body_area_height, padding=ft.padding.only(top=8),
                        ),
                        ft.Container(
                            ft.Column(
                                [
                                    ft.Row(
                                        (
                                            [ft.OutlinedButton("طباعة الباركود", icon=ft.Icons.QR_CODE_2, on_click=lambda _, i=data: (close(), open_barcode_print_dialog([(i, 1)], layout=barcode_settings.label_layout(ctx.settings), roll_width_mm=barcode_settings.label_roll_width_mm(ctx.settings))), expand=True)]
                                            if str(data.get("barcode") or "").strip() else []
                                        ) + [ft.OutlinedButton("بطاقة QR", icon=ft.Icons.SHARE_ROUNDED, on_click=lambda _, i=data: (close(), open_item_card_sheet(i)), expand=True)],
                                        spacing=10,
                                    ),
                                    ft.Row(
                                        [
                                            ft.OutlinedButton("تعديل", icon=ft.Icons.EDIT_OUTLINED, on_click=edit, expand=True),
                                            ft.FilledButton("إغلاق", on_click=close, expand=True),
                                        ] + (
                                            [ft.TextButton("حذف", icon=ft.Icons.DELETE_OUTLINE, on_click=delete_it)]
                                            if can_delete else []
                                        ),
                                        spacing=10,
                                    ),
                                ],
                                spacing=8,
                            ),
                            padding=ft.padding.only(top=4),
                        ),
                    ],
                    spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                height=total_sheet_h,
                padding=ft.padding.only(left=18, right=18, top=12, bottom=20),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=28, top_right=28),
                shadow=Shadow.LG,
            )
            page.open(detail_sheet)

        def confirm_delete_item(item: dict) -> None:
            # Reachable only for rows with has_activity == 0 (see refresh()
            # below — items with activity aren't wrapped in a Dismissible
            # at all), and delete() re-checks anyway, so a race is never
            # destructive: any failure just restores the row via refresh().
            confirm = ft.AlertDialog(modal=True)

            def close(_=None):
                page.close(confirm)
                refresh()  # restores the swiped-away row

            def remove(_=None):
                try:
                    ctx.items.delete(int(item["id"]))
                    page.close(confirm)
                    notify("تم حذف المادة", kind="success", sound_kind="delete")
                    refresh()
                except Exception as exc:
                    page.close(confirm)
                    notify(str(exc), kind="error")
                    refresh()

            confirm.title = ft.Text("حذف المادة")
            confirm.content = ft.Text(f"هل تريد حذف «{item['name']}»؟ لن يُسمح بالحذف إذا استُخدمت المادة لاحقًا.")
            confirm.actions = [ft.TextButton("إلغاء", on_click=close), ft.FilledButton("حذف", icon=ft.Icons.DELETE_FOREVER, on_click=remove)]
            page.open(confirm)

        def _quiet_item_ids() -> set[int]:
            """Stock item ids with no sale movements in the last 30 days."""
            try:
                with ctx.db.connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT it.id
                        FROM items it
                        WHERE it.item_type='مخزون'
                          AND NOT EXISTS (
                            SELECT 1 FROM inventory_movements im
                            WHERE im.item_id=it.id
                              AND im.movement_type='sale'
                              AND im.movement_date >= date('now', '-30 day')
                          )
                        """
                    ).fetchall()
                return {int(r[0] if not hasattr(r, 'keys') else r['id']) for r in rows}
            except Exception:
                return set()

        def refresh(_=None):
            all_items = ctx.items.list()
            query = (search.value or "").strip().casefold()
            filtered = [
                i for i in all_items
                if not query
                or query in str(i.get("name") or "").casefold()
                or query in str(i.get("barcode") or "").casefold()
            ]
            mode = filter_state["mode"]
            if mode == "stock":
                filtered = [i for i in filtered if i.get("item_type") == "مخزون"]
            elif mode == "service":
                filtered = [i for i in filtered if i.get("item_type") == "خدمة"]
            elif mode == "low":
                filtered = [i for i in filtered if i.get("item_type") == "مخزون" and float(i.get("quantity") or 0) < LOW_STOCK]
            elif mode == "no_barcode":
                filtered = [i for i in filtered if not str(i.get("barcode") or "").strip()]
            elif mode == "no_price":
                filtered = [
                    i for i in filtered
                    if i.get("item_type") == "مخزون" and float(i.get("selling_price") or 0) <= 0
                ]
            elif mode == "no_movement":
                # Items with stock but no sales movement in last 30 days
                quiet_ids = _quiet_item_ids()
                filtered = [
                    i for i in filtered
                    if i.get("item_type") == "مخزون"
                    and float(i.get("quantity") or 0) > 0
                    and int(i.get("id") or 0) in quiet_ids
                ]
            elif mode == "restock":
                try:
                    preds = self.ctx.dashboard.restock_predictions(limit=200)
                    pred_ids = {int(p["item_id"]) for p in preds}
                except Exception:
                    pred_ids = set()
                filtered = [i for i in filtered if int(i.get("id") or 0) in pred_ids]
            sort_key = sort_state["key"]
            if sort_key == "price_desc":
                filtered.sort(key=lambda i: float(i.get("selling_price") or 0), reverse=True)
            elif sort_key == "price_asc":
                filtered.sort(key=lambda i: float(i.get("selling_price") or 0))
            elif sort_key == "qty_asc":
                # Services have no meaningful quantity -- push them to the
                # end instead of letting their 0 look like "needs restock".
                filtered.sort(key=lambda i: (i.get("item_type") != "مخزون", float(i.get("quantity") or 0)))
            # "name" is the repository's own default ORDER BY i.name -- no
            # re-sort needed, and re-sorting would just be casefold(name)
            # busywork for the common case.
            # Header-click sort (DataGrid) runs last and overrides the preset
            # dropdown for the chosen column.
            if grid_sort["key"]:
                header_key = grid_sort["key"]
                reverse = grid_sort["dir"] == "desc"

                def header_sort_value(item):
                    value = item.get(header_key)
                    if header_key in ("quantity", "purchase_price", "selling_price"):
                        try:
                            return float(value or 0)
                        except (TypeError, ValueError):
                            return 0.0
                    return str(value or "")

                filtered.sort(key=header_sort_value, reverse=reverse)
            inventory_value = sum(float(i.get("quantity") or 0) * float(i.get("average_cost") or 0) for i in all_items if i.get("item_type") == "مخزون")
            low_count = sum(1 for i in all_items if i.get("item_type") == "مخزون" and float(i.get("quantity") or 0) < LOW_STOCK)
            service_count = sum(1 for i in all_items if i.get("item_type") == "خدمة")
            summary_row.controls = metric_controls([
                kpi_card("عدد المواد", str(len(all_items)), ft.Icons.INVENTORY_2_OUTLINED, Colors.PRIMARY),
                kpi_card("قيمة المخزون", money(inventory_value), ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.SUCCESS),
                kpi_card("مخزون منخفض", str(low_count), ft.Icons.WARNING_AMBER_ROUNDED, Colors.DANGER, on_tap=lambda _: set_filter("low")),
                kpi_card("الخدمات", str(service_count), ft.Icons.HANDYMAN_OUTLINED, Colors.PURPLE, on_tap=lambda _: set_filter("service")),
            ])
            # Desktop spreadsheet grid, driven by search/filters/sort.
            export_state["rows"] = filtered
            grid.set_hidden_keys(current_hidden())
            shown = filtered[: render_limit["n"]]
            grid.set_rows(shown)
            remaining = len(filtered) - len(shown)
            if remaining > 0:
                grid_more.content = ft.Row(
                    [ft.OutlinedButton(f"تحميل المزيد ({remaining})", icon=ft.Icons.EXPAND_MORE_ROUNDED,
                                       on_click=lambda _=None: (render_limit.__setitem__("n", render_limit["n"] + 100), refresh()))],
                    alignment=ft.MainAxisAlignment.CENTER,
                )
                grid_more.visible = True
            else:
                grid_more.visible = False
            list_switch.content = desktop_list
            update_filter_styles(); page.update()

        def refresh_from_search(e=None):
            # A new search term makes the old page size meaningless (it was
            # sized against a different, larger result set) -- start over
            # from the first page so "تحميل المزيد" reflects the new query.
            render_limit["n"] = 60
            refresh()

        search.on_change = refresh_from_search
        # Horizontally scrollable instead of wrap=True: on narrow phones the
        # old wrap=True Row let each chip's Container claim the full row
        # width (one chip per visual line -- "مكتظة جدا" / too crowded).
        # A single scrolling row keeps chips at their natural compact width
        # and puts sort_dd beside them instead of stacking everything.
        filters = ft.Row([
            filter_box("all", "الكل", ft.Icons.APPS_ROUNDED),
            filter_box("stock", "مخزون", ft.Icons.INVENTORY_2_OUTLINED),
            filter_box("service", "خدمات", ft.Icons.HANDYMAN_OUTLINED),
            filter_box("low", "منخفض", ft.Icons.WARNING_AMBER_ROUNDED),
            filter_box("restock", "سينفد", ft.Icons.SCHEDULE_ROUNDED),
            filter_box("no_barcode", "بلا باركود", ft.Icons.QR_CODE_2_OUTLINED),
            filter_box("no_price", "بلا سعر", ft.Icons.MONEY_OFF_OUTLINED),
            filter_box("no_movement", "بلا حركة", ft.Icons.HOURGLASS_EMPTY_ROUNDED),
        ], spacing=6, scroll=ft.ScrollMode.AUTO)
        sort_dd.width = 130
        # Same toolbar shape as parties_view: the search field shares one
        # row with the column/export/density actions instead of being
        # mounted on its own line where it stretched edge to edge.
        toolbar_row = ft.Row(
            [ft.Container(search, expand=True), columns_button, export_button, density_button],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4,
        )
        filters_row = ft.Row(
            [ft.Container(filters, expand=True), sort_dd],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4,
        )

        # Sticky header: only search + filters, always reachable while
        # scrolling a long item list -- everything else (stats, list) moves
        # to the scrolling body below it.
        sticky_top = ft.Container(
            ft.Column([toolbar_row, filters_row], spacing=10),
            padding=ft.padding.only(left=18, right=18, top=14, bottom=10),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
        )

        # Stats summary starts collapsed -- 4 stat cards above the list was
        # a lot of vertical space to pay on every visit just to reach the
        # actual items. Tap the header to expand/collapse.
        stats_expanded = {"value": False}
        stats_chevron = ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED, size=18, color=Colors.TEXT_SECONDARY)
        stats_body = ft.Container(summary_row, visible=stats_expanded["value"])

        def toggle_stats(_=None):
            stats_expanded["value"] = not stats_expanded["value"]
            stats_body.visible = stats_expanded["value"]
            stats_chevron.name = ft.Icons.KEYBOARD_ARROW_UP_ROUNDED if stats_expanded["value"] else ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED
            page.update()

        stats_header = ft.Container(
            ft.Row(
                [
                    ft.Icon(ft.Icons.BAR_CHART_ROUNDED, size=15, color=Colors.TEXT_SECONDARY),
                    ft.Text("ملخص المخزون", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY, expand=True),
                    stats_chevron,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=6,
            ),
            padding=ft.padding.symmetric(horizontal=2, vertical=4),
            on_click=toggle_stats, ink=True, border_radius=8,
        )

        # The stats block is mounted once (a control must not have two
        # parents); `list_switch` holds the desktop grid.
        desktop_list = ft.Column(
            [ft.Container(grid, expand=True), grid_more],
            spacing=8, expand=True,
        )
        list_switch = ft.Container(content=desktop_list, expand=True)
        list_area = ft.Container(
            ft.Column([stats_header, stats_body, bulk_bar, list_switch], spacing=8, expand=True),
            padding=ft.padding.only(left=18, right=18, top=14, bottom=10),
            expand=True,
        )

        content.content = ft.Column(
            [
                sticky_top,
                list_area,
            ],
            spacing=0, expand=True,
        )
        refresh()
        # Publish this section's contextual ribbon actions (desktop). The
        # shell resets them to its own defaults on the next navigation, so
        # these closures are only ever invoked while items is on screen.
        if self.on_context_actions is not None:
            self.on_context_actions([
                RibbonAction("new_item", "مادة جديدة", ft.Icons.ADD_ROUNDED, accent=True, on_click=lambda _=None: open_item_editor()),
                RibbonAction("bulk_add", "إضافة شاملة (جدول)", ft.Icons.VIEW_LIST_OUTLINED, on_click=lambda _=None: open_bulk_add_sheet()),
                RibbonAction("definitions", "تصنيفات ووحدات", ft.Icons.TUNE_ROUNDED, on_click=lambda _=None: open_definitions_sheet()),
                RibbonAction("bulk_barcode", "طباعة الباركودات", ft.Icons.QR_CODE_2, on_click=lambda _=None: open_bulk_barcode_sheet()),
                RibbonAction("bulk_edit", "تعديل جماعي", ft.Icons.EDIT_NOTE_OUTLINED, on_click=lambda _=None: open_bulk_edit_sheet()),
                *(
                    [RibbonAction("import_card", "استيراد بطاقة", ft.Icons.QR_CODE_SCANNER, on_click=lambda _=None: open_import_card_sheet())]
                    if supports("barcode_camera_scan")
                    else []
                ),
            ])
        if prefill_barcode:
            open_item_editor(None, prefill_barcode)


__all__ = ["ItemsCenter"]
