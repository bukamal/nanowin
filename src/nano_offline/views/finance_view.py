from __future__ import annotations

import csv as _csv
import tempfile
from datetime import date, datetime
from pathlib import Path

import flet as ft

from nano_offline.core.toast import toast

from nano_offline.components import SearchSelect, SelectAllTextField, SmartAmountField, SmartDateField, empty_state, kpi_card, new_form_sheet, render_form_sheet, metric_row, metric_controls, DataGrid, GridColumn
from nano_offline.components.command_bar import RibbonAction
from nano_offline.components.column_visibility import ColumnVisibility
from nano_offline.components.money_text import money_row
from nano_offline.components.print_profile import apply_print_profile, open_print_profile_dialog
from nano_offline.services.statement_service import StatementService
from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.core import currency
from nano_offline.core.platform_capabilities import feature_status
from nano_offline.core.expiring import invalidate_insights
from nano_offline.core.home_widget import refresh_home_widget


# Compatibility terminology: «سند دفع» هو نفسه «سند صرف» في الإصدارات السابقة.

# Desktop grid columns for the vouchers screen (قبض/دفع). Both the
# allocated and unallocated shares are shown side by side so the
# "على الحساب" leftovers that need follow-up are one glance away.
VOUCHER_GRID_COLUMNS = [
    GridColumn("id", "السند", width=80, align="center", kind="number", tooltip="رقم السند — اضغط للترتيب"),
    GridColumn("voucher_type", "النوع", width=80, align="center", kind="badge", tooltip="قبض (من الحساب) أو دفع (إلى الحساب)"),
    GridColumn("party_name", "الحساب", tooltip="الطرف المرتبط بالسند — نقدي عند عدم التحديد"),
    GridColumn("voucher_date", "التاريخ", width=98, align="center", tooltip="تاريخ السند — يُخفى تلقائيًا على الشاشات الضيقة"),
    GridColumn("amount", "المبلغ", width=110, align="end", kind="money", tooltip="قيمة السند الإجمالية"),
    GridColumn("allocated_amount", "موزّع", width=100, align="end", kind="money", tooltip="الجزء الموزّع على فواتير"),
    GridColumn("unallocated_amount", "على الحساب", width=110, align="end", kind="money", tooltip="رصيد غير موزّع — الصف مُظلَّل عند وجوده"),
]

# Desktop grid columns for the expenses screen.
EXPENSE_GRID_COLUMNS = [
    GridColumn("description", "البيان", tooltip="وصف المصروف — اضغط للترتيب"),
    GridColumn("category_name", "التصنيف", width=140, align="center", kind="badge", tooltip="تصنيف المصروف"),
    GridColumn("expense_date", "التاريخ", width=100, align="center", tooltip="تاريخ المصروف"),
    GridColumn("reference", "المرجع", width=130, tooltip="رقم أو مرجع المستند — يُخفى تلقائيًا على الشاشات الضيقة"),
    GridColumn("_receipt", "الإيصال", width=80, align="center", kind="badge", sortable=False, tooltip="صورة إيصال مرفقة"),
    GridColumn("amount", "المبلغ", width=120, align="end", kind="money", tooltip="قيمة المصروف"),
]


def _detail_chip(icon, label: str, value: str) -> ft.Control:
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

class FinanceCenter:
    def __init__(self, page: ft.Page, ctx, content: ft.Container, on_changed=None, native_files=None, on_title_change=None, on_open_invoice=None, on_context_actions=None):
        self.page = page
        self.ctx = ctx
        self.content = content
        self.on_changed = on_changed
        self.native_files = native_files
        self.on_title_change = on_title_change
        # Drill-down from statement rows into the source document --
        # wired in main.py to the invoice center (same pattern as
        # parties_view's on_open_receipt/on_open_payment callbacks).
        self.on_open_invoice = on_open_invoice
        # Publishes this section's contextual ribbon actions while finance is
        # on screen (سند قبض / سند دفع / سند مصروف).
        self.on_context_actions = on_context_actions

    def money(self, value) -> str:
        return currency.format_amount(value, self.ctx.settings)

    def notify(self, text: str, kind: str | None = None, sound_kind: str | None = None) -> None:
        toast(self.page, text, kind=kind, sound_kind=sound_kind)

    def _changed(self) -> None:
        # Vouchers/expenses feed the dashboard's cached advisory block
        # (receivables pulse, alerts); drop the cache on every write.
        invalidate_insights()
        if self.on_changed:
            self.on_changed()

    def _set_header(self, title: str, subtitle: str = "") -> None:
        if self.on_title_change:
            self.on_title_change(title, subtitle)

    def _nav_chip(self, label: str, icon, *, active: bool, on_click) -> ft.Container:
        # Same pill language as the filter chips used on this screen (and
        # on items_view's list) -- a page-level tab bar built out of
        # ft.FilledButton/OutlinedButton mixed with those chips read as two
        # different button systems stacked on top of each other. One chip
        # style throughout keeps the section switcher visually part of the
        # same screen instead of a separate toolbar bolted on top of it.
        return ft.Container(
            ft.Row(
                [
                    ft.Icon(icon, size=14, color=Colors.WHITE if active else Colors.TEXT_MUTED),
                    ft.Text(label, size=11, weight=ft.FontWeight.W_600, color=Colors.WHITE if active else Colors.TEXT_MUTED),
                ],
                spacing=5, tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=12, vertical=9),
            bgcolor=Colors.PRIMARY if active else Colors.WHITE,
            border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
            border_radius=20, ink=True,
            on_click=lambda _: on_click(),
        )

    def _section_nav(self, active: str) -> ft.Row:
        items = [
            ("vouchers", "السندات", ft.Icons.RECEIPT_LONG_OUTLINED, self.show_vouchers),
            ("expenses", "المصروفات", ft.Icons.PAYMENTS_OUTLINED, self.show_expenses),
            ("customers", "كشف العملاء", ft.Icons.PEOPLE_ALT_OUTLINED, lambda: self.show_statements("customer")),
            ("suppliers", "كشف الموردين", ft.Icons.LOCAL_SHIPPING_OUTLINED, lambda: self.show_statements("supplier")),
            ("ledger", "دفتر الأستاذ", ft.Icons.DESCRIPTION_OUTLINED, self.show_ledger),
        ]
        return ft.Row(
            [self._nav_chip(label, icon, active=key == active, on_click=action) for key, label, icon, action in items],
            wrap=True, spacing=8, run_spacing=8,
        )

    def _summary_toggle(self, summary: ft.Control) -> ft.OutlinedButton:
        visible = {"value": self.ctx.settings.get("ui.finance_summary_visible", "true").strip().lower() in ("1", "true", "yes", "on")}
        summary.visible = visible["value"]

        def toggle(_=None):
            visible["value"] = not visible["value"]
            summary.visible = visible["value"]
            self.ctx.settings.set("ui.finance_summary_visible", str(visible["value"]).lower())
            button.text = "إخفاء الملخص" if visible["value"] else "إظهار الملخص"
            button.icon = ft.Icons.VISIBILITY_OFF_OUTLINED if visible["value"] else ft.Icons.INSIGHTS_OUTLINED
            self.page.update()

        button = ft.OutlinedButton(
            "إخفاء الملخص" if visible["value"] else "إظهار الملخص",
            icon=ft.Icons.VISIBILITY_OFF_OUTLINED if visible["value"] else ft.Icons.INSIGHTS_OUTLINED,
            on_click=toggle,
        )
        return button

    def show_center(self) -> None:
        self.show_vouchers()

    def show_vouchers(self) -> None:
        self._set_header("السندات", "سندات القبض والدفع والمصروفات وربطها بالفواتير")
        search = SelectAllTextField(
            label="بحث في السندات",
            hint_text="رقم السند، الحساب أو المرجع",
            prefix_icon=ft.Icons.SEARCH,
            on_focus=lambda e: grid.disarm(),
        )
        summary = ft.ResponsiveRow(spacing=8, run_spacing=8)
        summary_toggle = self._summary_toggle(summary)
        filter_state = {"type": "all"}
        # Same client-visible paging as parties/items/invoices (see
        # parties_view.py): vouchers only accumulate over the life of the
        # business, so building a card for every single one on every
        # refresh does not scale. Render a page at a time instead.
        render_limit = {"n": 60}
        # Real dropdown (ft.Dropdown) instead of a search-style field or a
        # row of chips -- same control used for "ترتيب حسب" on the items
        # screen: tap once, pick from the list, no typing involved.
        type_filter = ft.Dropdown(
            label="نوع السند",
            value="all",
            options=[
                ft.dropdown.Option(key="all", text="الكل"),
                ft.dropdown.Option(key="receipt", text="قبض"),
                ft.dropdown.Option(key="payment", text="دفع"),
            ],
            filled=True,
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.MD,
            border_color=Colors.BORDER,
        )

        def set_filter(key: str):
            filter_state["type"] = key
            if type_filter.value != key:
                type_filter.value = key
            render_limit["n"] = 60
            refresh()

        def on_type_filter_change(_=None):
            filter_state["type"] = type_filter.value or "all"
            render_limit["n"] = 60
            refresh()

        type_filter.on_change = on_type_filter_change

        # ---- desktop grid (same spreadsheet treatment as items/invoices) --
        grid_sort = {"key": None, "dir": None}
        density_state = {"compact": self.ctx.settings.get("ui.vouchers_density", "comfortable") == "compact"}
        selected_state = {"rows": []}
        export_state = {"rows": []}
        columns_visibility = ColumnVisibility(
            columns=VOUCHER_GRID_COLUMNS,
            settings=self.ctx.settings,
            preference_key="ui.vouchers_hidden_columns",
            auto_hidden={"voucher_date", "unallocated_amount"} if (self.page.width or 1200) < 720 else set(),
            on_change=lambda: (
                grid.set_hidden_keys(columns_visibility.hidden_keys),
                self.page.update(),
            ),
        )
        columns_button = columns_visibility.button()

        async def export_csv(_=None) -> None:
            rows_out = export_state["rows"]
            if not rows_out:
                self.notify("لا توجد بيانات للتصدير", kind="warning")
                return
            if self.native_files is None:
                self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                path = Path(tempfile.gettempdir()) / f"nano_vouchers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = _csv.writer(fh)
                    writer.writerow(["السند", "النوع", "الحساب", "التاريخ", "المبلغ", "موزّع", "على الحساب", "المرجع"])
                    for v in rows_out:
                        writer.writerow([
                            v.get("id") or "",
                            "قبض" if v.get("voucher_type") == "receipt" else "دفع",
                            v.get("party_name") or "نقدي",
                            v.get("voucher_date") or "",
                            v.get("amount") or 0,
                            v.get("allocated_amount") or 0,
                            v.get("unallocated_amount") or 0,
                            v.get("reference") or "",
                        ])
                await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير السندات")
                self.notify("تم تجهيز ملف CSV (بترميز يفتح في Excel العربي)")
            except Exception as exc:
                self.notify(str(exc), kind="error")

        export_button = ft.IconButton(
            icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
            tooltip="تصدير CSV للعرض الحالي",
            on_click=export_csv,
        )

        def _grid_formatter(col, value, row):
            if col.key == "voucher_type":
                if row.get("voucher_type") == "receipt":
                    return ("قبض", Colors.SUCCESS, Colors.SUCCESS_BG)
                return ("دفع", Colors.DANGER_DARK, Colors.DANGER_BG)
            if col.key == "party_name":
                return str(value or "نقدي")
            if col.kind == "money":
                return self.money(value)
            return None

        def voucher_row_bg(row: dict) -> str | None:
            if float(row.get("unallocated_amount") or 0) > 1e-9:
                return Colors.WARNING_BG_ALT
            return None

        def voucher_expand_panel(row: dict) -> ft.Control:
            return ft.Row(
                [
                    _detail_chip(ft.Icons.TAG_OUTLINED, "المرجع", str(row.get("reference") or "—")),
                    _detail_chip(ft.Icons.CALENDAR_MONTH_OUTLINED, "التاريخ", str(row.get("voucher_date") or "—")),
                    _detail_chip(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, "موزّع", self.money(row.get("allocated_amount"))),
                ],
                spacing=8, wrap=True,
            )

        def duplicate_voucher(row: dict) -> None:
            source = self.ctx.payments.get_voucher(int(row.get("id") or 0))
            if source:
                self.show_voucher_dialog(None, source.get("voucher_type") or "receipt", initial_data=source)

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

        def render_bulk_bar(rows_out: list[dict]) -> None:
            selected_state["rows"] = list(rows_out)
            if not rows_out:
                bulk_bar.visible = False
                bulk_bar.content = None
            else:
                bulk_bar.content = ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECK_BOX, size=16, color=Colors.PRIMARY),
                        ft.Text(f"تم تحديد {len(rows_out)}", size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK),
                        ft.Container(expand=True),
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
            chosen = list(selected_state["rows"])
            if not chosen:
                return
            dialog = ft.AlertDialog(modal=True, title=ft.Text("تأكيد حذف السندات"))

            def yes(_ev=None) -> None:
                self.page.close(dialog)
                deleted = 0
                failures = 0
                for voucher in chosen:
                    try:
                        self.ctx.payments.delete_voucher(int(voucher.get("id") or 0))
                        deleted += 1
                    except Exception:
                        failures += 1
                if failures:
                    self.notify(f"حُذف {deleted}، وتعذّر حذف {failures}", kind="warning")
                else:
                    self.notify(f"تم حذف {deleted} وعكس أثرها المالي", sound_kind="success")
                self.show_vouchers()
                self._changed()

            def no(_ev=None) -> None:
                self.page.close(dialog)

            dialog.content = ft.Text(f"سيتم حذف {len(chosen)} سند وتوزيعاتها وإعادة احتساب الذمم. هل تريد المتابعة؟")
            dialog.actions = [
                ft.TextButton("إلغاء", on_click=no),
                ft.FilledButton("حذف", on_click=yes, style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE)),
            ]
            self.page.open(dialog)

        def toggle_density(_=None) -> None:
            compact = not density_state["compact"]
            density_state["compact"] = compact
            grid.set_row_height(36 if compact else 48)
            density_button.icon = ft.Icons.DENSITY_SMALL if compact else ft.Icons.DENSITY_MEDIUM
            density_button.tooltip = "عرض مريح" if compact else "عرض مضغوط"
            self.ctx.settings.set("ui.vouchers_density", "compact" if compact else "comfortable")
            self.page.update()

        density_button = ft.IconButton(
            icon=ft.Icons.DENSITY_SMALL if density_state["compact"] else ft.Icons.DENSITY_MEDIUM,
            tooltip="عرض مريح" if density_state["compact"] else "عرض مضغوط",
            on_click=toggle_density,
        )
        bulk_bar = ft.Container(visible=False)

        def voucher_menu(row: dict) -> list:
            voucher_id = int(row.get("id") or 0)
            return [
                ("فتح", ft.Icons.OPEN_IN_NEW_ROUNDED, lambda i=voucher_id: self.show_voucher_detail(i)),
                ("تعديل", ft.Icons.EDIT_OUTLINED, lambda i=voucher_id: self.show_voucher_dialog(i)),
                ("تكرار", ft.Icons.CONTENT_COPY_OUTLINED, lambda r=row: duplicate_voucher(r)),
                None,
                ("حذف", ft.Icons.DELETE_OUTLINE, lambda i=voucher_id: self.confirm_delete_voucher(i)),
            ]

        grid_height = max(300, min(560, int((self.page.height or 760) * 0.52)))
        grid = DataGrid(
            columns=VOUCHER_GRID_COLUMNS,
            rows=[],
            page=self.page,
            row_height=36 if density_state["compact"] else 48,
            viewport_height=grid_height,
            hidden_keys=columns_visibility.hidden_keys,
            on_row_activate=lambda row: self.show_voucher_detail(int(row.get("id") or 0)),
            on_sort=apply_grid_sort,
            selectable=True,
            on_selection_change=render_bulk_bar,
            on_row_menu=voucher_menu,
            row_bg=voucher_row_bg,
            on_row_expand=voucher_expand_panel,
            on_search_shortcut=focus_search,
            on_new_shortcut=lambda: self.show_voucher_dialog(None, "receipt"),
            formatter=_grid_formatter,
            boxed=True,
            center_cells=True,
            activate_tooltip="فتح السند",
            empty_text="لا توجد سندات",
        )
        type_filter.on_focus = lambda e: grid.disarm()
        grid_more = ft.Container(visible=False)
        desktop_list = ft.Column([bulk_bar, ft.Container(grid), grid_more], spacing=8)
        list_switch = ft.Container(content=desktop_list)

        def refresh(_=None):
            vouchers = self.ctx.payments.list_vouchers()
            receipts = sum(float(v.get("amount") or 0) for v in vouchers if v.get("voucher_type") == "receipt")
            payments = sum(float(v.get("amount") or 0) for v in vouchers if v.get("voucher_type") == "payment")
            unallocated_total = sum(float(v.get("unallocated_amount") or 0) for v in vouchers)
            summary.controls = metric_controls([
                kpi_card("إجمالي القبض", "", ft.Icons.ADD_CARD, Colors.SUCCESS, on_tap=lambda _: set_filter("receipt"), amount_usd=receipts, settings=self.ctx.settings),
                kpi_card("إجمالي الدفع", "", ft.Icons.PAYMENTS_OUTLINED, Colors.DANGER, on_tap=lambda _: set_filter("payment"), amount_usd=payments, settings=self.ctx.settings),
                kpi_card("رصيد غير موزع", "", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.PRIMARY, amount_usd=unallocated_total, settings=self.ctx.settings),
            ])
            q = (search.value or "").strip().casefold()
            filtered = []
            for v in vouchers:
                if filter_state["type"] != "all" and v.get("voucher_type") != filter_state["type"]:
                    continue
                hay = f"{v.get('id','')} {v.get('party_name','')} {v.get('reference','')}".casefold()
                if q and q not in hay:
                    continue
                filtered.append(v)
            if grid_sort["key"]:
                sort_key = grid_sort["key"]
                reverse = grid_sort["dir"] == "desc"

                def sort_value(voucher):
                    if sort_key in ("id", "amount", "allocated_amount", "unallocated_amount"):
                        try:
                            return float(voucher.get(sort_key) or 0)
                        except (TypeError, ValueError):
                            return 0.0
                    return str(voucher.get(sort_key) or "")

                filtered.sort(key=sort_value, reverse=reverse)
            export_state["rows"] = filtered
            grid.set_hidden_keys(columns_visibility.hidden_keys)
            if not filtered:
                grid.set_rows([])
                if vouchers:
                    list_switch.content = empty_state(
                        "لا توجد سندات مطابقة",
                        icon=ft.Icons.RECEIPT_LONG_OUTLINED,
                        hint="جرّب تغيير البحث أو عوامل التصفية",
                    )
                else:
                    list_switch.content = empty_state(
                        "لا توجد سندات بعد",
                        icon=ft.Icons.RECEIPT_LONG_OUTLINED,
                        hint="أنشئ أول سند قبض أو دفع لتتبع حركة الصندوق",
                        action_label="سند قبض جديد",
                        on_action=lambda _: self.show_voucher_dialog(None, "receipt"),
                    )
                self.page.update()
                return
            shown = filtered[: render_limit["n"]]
            grid.set_rows(shown)
            remaining = len(filtered) - len(shown)
            if remaining > 0:
                grid_more.content = ft.Row(
                    [ft.OutlinedButton(
                        f"تحميل المزيد ({remaining})", icon=ft.Icons.EXPAND_MORE_ROUNDED,
                        on_click=lambda _=None: (render_limit.__setitem__("n", render_limit["n"] + 60), refresh())),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                )
                grid_more.visible = True
            else:
                grid_more.visible = False
            list_switch.content = desktop_list
            self.page.update()

        def refresh_from_search(_=None):
            render_limit["n"] = 60
            refresh()

        search.on_change = refresh_from_search

        # Same sticky-header / scrolling-body split as invoice_view and
        # items_view: the section tabs, search, and type filters live in a
        # bordered white bar that stays put, while the KPI cards and the
        # voucher list scroll together underneath it. Search shares its row
        # with the type dropdown + actions (parties_view toolbar shape)
        # instead of spanning the whole width by itself.
        type_filter.width = 130
        sticky_header = ft.Container(
            ft.Column(
                [
                    self._section_nav("vouchers"),
                    ft.Row(
                        [ft.Container(search, expand=True), type_filter, columns_button, export_button, density_button],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row([summary_toggle], alignment=ft.MainAxisAlignment.END),
                ],
                spacing=10,
            ),
            padding=ft.padding.only(left=16, right=16, top=14, bottom=12),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        scroll_body = ft.Container(
            ft.Column(
                [
                    summary,
                    list_switch,
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            expand=True,
            padding=ft.padding.only(top=12, left=16, right=16),
        )
        body = ft.Column([sticky_header, scroll_body], spacing=0, expand=True)
        self.content.content = body
        # Creation actions live in the shell's contextual ribbon now (the old
        # floating speed-dial was mobile chrome): one button per voucher type
        # so قبض / دفع / مصروف stay one click away from any finance tab.
        if self.on_context_actions is not None:
            self.on_context_actions([
                RibbonAction("new_receipt", "سند قبض", ft.Icons.ADD_CARD, accent=True, on_click=lambda _=None: self.show_voucher_dialog(None, "receipt")),
                RibbonAction("new_payment", "سند دفع", ft.Icons.PAYMENTS_OUTLINED, on_click=lambda _=None: self.show_voucher_dialog(None, "payment")),
                RibbonAction("new_expense", "سند مصروف", ft.Icons.RECEIPT_OUTLINED, on_click=lambda _=None: self.show_expense_dialog()),
            ])
        refresh()

    def _voucher_html(self, voucher: dict) -> str:
        receipt = voucher.get("voucher_type") == "receipt"
        title = "سند قبض" if receipt else "سند دفع"
        accent = Colors.SUCCESS_ALT if receipt else Colors.DANGER_DARK
        party_label = "العميل" if receipt else "المورد"
        return f"""<!doctype html><html dir='rtl' lang='ar'><head><meta charset='utf-8'><style>
        @page {{ size:80mm auto; margin:0; }} * {{ box-sizing:border-box; }} body {{ width:80mm; margin:0; padding:5mm; font-family:Arial,sans-serif; color:#111; }}
        .brand {{ text-align:center; color:#0F766E; font-size:20px; font-weight:800; }} .sub {{ text-align:center; font-size:10px; color:#64748B; }}
        .type {{ text-align:center; font-size:15px; font-weight:700; margin:8px 0; }} .amount {{ text-align:center; color:{accent}; font-size:24px; font-weight:900; margin:10px 0; }}
        .line {{ border-top:1px dashed #777; margin:8px 0; }} .row {{ display:flex; justify-content:space-between; gap:8px; margin:5px 0; font-size:11px; }}
        .label {{ color:#64748B; }} .footer {{ text-align:center; color:#94A3B8; font-size:9px; margin-top:12px; }}
        </style></head><body><div class='brand'>Nano | نانو</div><div class='sub'>نظام المحاسبة الذكي</div><div class='type'>{title} #{voucher.get('id')}</div>
        <div class='amount'>{self.money(voucher.get('amount'))}</div><div class='line'></div>
        <div class='row'><span class='label'>التاريخ</span><strong>{voucher.get('voucher_date') or '—'}</strong></div>
        <div class='row'><span class='label'>{party_label}</span><strong>{voucher.get('party_name') or '—'}</strong></div>
        <div class='row'><span class='label'>المرجع</span><strong>{voucher.get('reference') or '—'}</strong></div>
        <div class='row'><span class='label'>موزع</span><strong>{self.money(voucher.get('allocated_amount'))}</strong></div>
        <div class='row'><span class='label'>على الحساب</span><strong>{self.money(voucher.get('unallocated_amount'))}</strong></div>
        <div class='line'></div><div style='font-size:10px'>{voucher.get('notes') or ''}</div><div class='footer'>تم إنشاء السند بواسطة Nano</div></body></html>"""

    def _open_payment_source(self, payment_id: int) -> None:
        """Drill-down for a ledger 'payment' movement: a real voucher opens
        its detail sheet, while an invoice's own initial payment (a derived
        reflection row, not a real voucher -- and its id lives in a
        different id space than vouchers) opens the invoice itself, so a
        tap can never land on an unrelated voucher that merely shares the
        numeric id."""
        try:
            with self.ctx.db.connect() as conn:
                prow = conn.execute(
                    "SELECT source_type, source_id FROM payments WHERE id=?", (payment_id,)
                ).fetchone()
        except Exception:
            prow = None
        if prow is not None and str(prow["source_type"]) == "invoice_initial" and prow["source_id"] is not None:
            if self.on_open_invoice is not None:
                try:
                    self.on_open_invoice(int(prow["source_id"]))
                    return
                except (TypeError, ValueError):
                    pass
        self.show_voucher_detail(payment_id)

    def show_voucher_detail(self, voucher_id: int) -> None:
        voucher = self.ctx.payments.get_voucher(voucher_id)
        if not voucher:
            self.notify("السند غير موجود"); return
        receipt = voucher.get("voucher_type") == "receipt"
        title = "سند قبض" if receipt else "سند دفع"
        accent = Colors.SUCCESS if receipt else Colors.DANGER_DARK
        allocations = voucher.get("allocations") or []
        alloc_controls = []
        for a in allocations:
            alloc_controls.append(ft.Container(ft.Row([ft.Text(f"فاتورة #{a['invoice_id']}", size=11, expand=True), money_row(a.get("amount"), self.ctx.settings, size=11, weight=ft.FontWeight.BOLD)]), padding=7, bgcolor=Colors.BACKGROUND, border_radius=10))
        if not alloc_controls:
            alloc_controls.append(ft.Text("لا توجد توزيعات على فواتير", size=10, color=Colors.TEXT_SECONDARY))
        # Bottom sheet instead of a centered AlertDialog -- same read-mostly
        # detail treatment used for item detail (items_view.py) and
        # customer/supplier detail (parties_view.py): opened by tapping a
        # row in a list, no destructive action of its own.
        detail_sheet = ft.BottomSheet(content=ft.Container(), enable_drag=True, maintain_bottom_view_insets_padding=True)

        def close(_=None): self.page.close(detail_sheet)
        def edit(_=None): close(); self.show_voucher_dialog(voucher_id)
        def duplicate(_=None):
            close(); self.show_voucher_dialog(None, voucher.get("voucher_type") or "receipt", initial_data=voucher)
        async def print_voucher(_=None):
            if self.native_files is None:
                self.notify("الطباعة الأصلية غير مهيأة في هذا البناء"); return

            async def print_selected(layout: str, width: int) -> None:
                try:
                    html = apply_print_profile(self._voucher_html(voucher), layout, width)
                    await self.native_files.print_html(
                        html,
                        name=f"nano-voucher-{voucher_id}",
                        paper_layout=layout,
                        roll_width_mm=width,
                    )
                except Exception as exc:
                    self.notify(str(exc), kind="error")

            open_print_profile_dialog(self.page, self.ctx.settings, print_selected)

        # Same explicit-height approach as the other converted detail
        # sheets: outer Container height computed from the real screen
        # height, scrollable body, fixed action footer (wraps onto a
        # second line on narrow screens since this one has five actions).
        page_h = self.page.height or 780
        total_sheet_h = min(int(page_h * 0.55), 600)
        header_reserved, footer_reserved, gaps = 70, 70, 24
        body_area_height = max(160, total_sheet_h - header_reserved - footer_reserved - gaps)

        detail_sheet.content = ft.Container(
            ft.Column(
                [
                    ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                    ft.Text(f"{title} #{voucher_id}", size=18, weight=ft.FontWeight.BOLD),
                    ft.Container(
                        ft.Column([
                            ft.Container(ft.Column([money_row(voucher.get("amount"), self.ctx.settings, size=28, weight=ft.FontWeight.BOLD, color=accent), ft.Text(title, size=11, color=Colors.TEXT_SECONDARY)], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2), padding=14, bgcolor=Colors.BACKGROUND, border_radius=16),
                            ft.ResponsiveRow([
                                ft.Container(ft.Text(f"التاريخ: {voucher.get('voucher_date') or '—'}", size=11), col={"xs": 6}),
                                ft.Container(ft.Text(f"الحساب: {voucher.get('party_name') or '—'}", size=11), col={"xs": 6}),
                                ft.Container(ft.Text(f"المرجع: {voucher.get('reference') or '—'}", size=11), col={"xs": 6}),
                                ft.Container(ft.Row([ft.Text("على الحساب: ", size=11), money_row(voucher.get("unallocated_amount"), self.ctx.settings, size=11)], spacing=2, tight=True), col={"xs": 6}),
                            ]),
                            ft.Text(voucher.get("notes") or "بدون ملاحظات", size=10, color=Colors.TEXT_SECONDARY),
                            ft.Divider(height=8), ft.Text("التوزيعات", size=13, weight=ft.FontWeight.BOLD), ft.Column(alloc_controls, spacing=5),
                        ], spacing=8, scroll=ft.ScrollMode.AUTO),
                        height=body_area_height, padding=ft.padding.only(top=4),
                    ),
                    ft.Container(
                        ft.Row(
                            [
                                ft.TextButton("حذف", icon=ft.Icons.DELETE_OUTLINE, on_click=lambda _: (close(), self.confirm_delete_voucher(voucher_id))),
                                ft.TextButton("تكرار", icon=ft.Icons.CONTENT_COPY, on_click=duplicate),
                                ft.OutlinedButton("طباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=print_voucher),
                                ft.OutlinedButton("تعديل", icon=ft.Icons.EDIT_OUTLINED, on_click=edit),
                                ft.FilledButton("إغلاق", on_click=close),
                            ],
                            spacing=8, run_spacing=8, wrap=True,
                        ),
                        padding=ft.padding.only(top=4),
                    ),
                ],
                spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            height=total_sheet_h,
            padding=ft.padding.only(left=18, right=18, top=12, bottom=20),
            bgcolor=Colors.WHITE,
            border_radius=ft.border_radius.only(top_left=28, top_right=28),
            shadow=Shadow.LG,
        )
        self.page.open(detail_sheet)

    def show_voucher_dialog(self, voucher_id: int | None = None, voucher_type: str = "receipt", initial_data: dict | None = None) -> None:
        existing = self.ctx.payments.get_voucher(voucher_id) if voucher_id else None
        seed = existing or initial_data or {}
        if voucher_id and not existing:
            self.notify("السند غير موجود")
            return

        current_type = existing["voucher_type"] if existing else seed.get("voucher_type", voucher_type)
        type_dd = SearchSelect(
            label="نوع السند",
            value=current_type,
            choices=[("receipt", "قبض من عميل"), ("payment", "صرف لمورد")],
            allow_clear=False,
        )
        party_dd = SearchSelect(label="الحساب")
        amount = SmartAmountField(
            label=currency.amount_field_label("المبلغ", self.ctx.settings),
            value=currency.to_input_text(seed.get("amount", 0), self.ctx.settings),
        )
        vdate = SmartDateField(label="التاريخ", value=str(seed.get("voucher_date") or date.today().isoformat()))
        reference = SelectAllTextField(label="المرجع", value=seed.get("reference") or "")
        notes = SelectAllTextField(label="ملاحظات", value=seed.get("notes") or "", multiline=True, min_lines=1, max_lines=3)
        existing_allocations = {int(a["invoice_id"]): float(a["amount"]) for a in (existing.get("allocations") or [])} if existing else {}
        default_mode = "manual" if existing_allocations else "none" if existing else "oldest"
        allocation_mode = SearchSelect(
            label="توزيع الدفعة",
            value=default_mode,
            choices=[
                ("oldest", "الأقدم أولًا تلقائيًا"),
                ("manual", "توزيع يدوي"),
                ("none", "رصيد على الحساب دون توزيع"),
            ],
            allow_clear=False,
        )
        manual_column = ft.Column(spacing=6)
        manual_fields: dict[int, ft.TextField] = {}

        def party_type() -> str:
            return "customer" if type_dd.value == "receipt" else "supplier"

        def load_parties(reset: bool = False) -> None:
            source = self.ctx.customers.list() if type_dd.value == "receipt" else self.ctx.suppliers.list()
            party_dd.label = "العميل" if type_dd.value == "receipt" else "المورد"
            party_dd.set_choices([(str(p["id"]), p["name"]) for p in source])
            if reset:
                party_dd.value = None
            elif seed and seed.get("voucher_type") == type_dd.value:
                pid = seed.get("customer_id") if type_dd.value == "receipt" else seed.get("supplier_id")
                party_dd.value = str(pid) if pid else None

        def refresh_manual(_=None) -> None:
            manual_column.controls = []
            manual_fields.clear()
            if allocation_mode.value != "manual":
                self.page.update()
                return
            if not party_dd.value:
                manual_column.controls.append(ft.Text("اختر الحساب أولًا لعرض الفواتير المفتوحة.", size=12, color=Colors.TEXT_SECONDARY))
                self.page.update()
                return
            exclude_payment_id = int(existing["payment_id"]) if existing and existing.get("payment_id") else None
            invoices = self.ctx.payments.allocatable_invoices(
                party_type(), int(party_dd.value), exclude_payment_id=exclude_payment_id
            )
            if not invoices:
                manual_column.controls.append(ft.Text("لا توجد فواتير قابلة للتوزيع.", size=12, color=Colors.TEXT_SECONDARY))
            for inv in invoices:
                iid = int(inv["id"])
                field = SmartAmountField(
                    label=f"توزيع على فاتورة #{iid}",
                    value=currency.to_input_text(existing_allocations.get(iid, 0), self.ctx.settings),
                    width=170,
                )
                manual_fields[iid] = field
                manual_column.controls.append(
                    ft.Container(
                        ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(f"فاتورة #{iid}", weight=ft.FontWeight.BOLD),
                                        ft.Row(
                                            [
                                                ft.Text(f"{inv['invoice_date']} • المتاح للتوزيع ", size=11, color=Colors.TEXT_SECONDARY),
                                                money_row(inv["allocatable_amount"], self.ctx.settings, size=11, color=Colors.TEXT_SECONDARY),
                                            ],
                                            spacing=2, tight=True,
                                        ),
                                    ],
                                    expand=True,
                                    spacing=2,
                                ),
                                field,
                            ],
                            wrap=True,
                        ),
                        padding=8,
                        border=ft.border.all(1, Colors.BORDER_ALT),
                        border_radius=8,
                    )
                )
            self.page.update()

        def type_changed(_):
            load_parties(reset=True)
            refresh_manual()

        type_dd.on_change = type_changed
        party_dd.on_change = refresh_manual
        allocation_mode.on_change = refresh_manual
        load_parties()

        sheet = new_form_sheet()

        def close(_=None):
            self.page.close(sheet)

        def save(_):
            try:
                amount_value = currency.parse_display_input(amount.value, self.ctx.settings)
                pid = int(party_dd.value) if party_dd.value else None
                kwargs = {
                    "voucher_type": type_dd.value,
                    "amount": amount_value,
                    "voucher_date": vdate.value,
                    "reference": reference.value,
                    "notes": notes.value,
                    "allocation_mode": allocation_mode.value,
                    "allocations": {
                        iid: currency.parse_display_input(field.value, self.ctx.settings)
                        for iid, field in manual_fields.items()
                    } if allocation_mode.value == "manual" else None,
                    "customer_id": pid if type_dd.value == "receipt" else None,
                    "supplier_id": pid if type_dd.value == "payment" else None,
                }
                if voucher_id:
                    self.ctx.payments.update_voucher(voucher_id, **kwargs)
                    message = f"تم تعديل السند #{voucher_id}"
                else:
                    new_id = self.ctx.payments.create_voucher(**kwargs)
                    message = f"تم حفظ السند #{new_id}"
                close()
                self.notify(message)
                self.show_vouchers()
                self._changed()
                # PHASE10: receipt/payment vouchers move the cash balance
                # shown on the home screen widget -- refresh it right away.
                refresh_home_widget(self.page, self.native_files, self.ctx.dashboard, self.ctx.settings)
            except Exception as exc:
                self.notify(str(exc), kind="error")

        render_form_sheet(
            self.page, sheet,
            title="تعديل سند" if voucher_id else "سند جديد",
            fields=[
                ft.ResponsiveRow(
                    [
                        ft.Container(type_dd, col={"xs": 12, "md": 6}),
                        ft.Container(party_dd, col={"xs": 12, "md": 6}),
                        ft.Container(amount, col={"xs": 6, "md": 4}),
                        ft.Container(vdate, col={"xs": 6, "md": 4}),
                        ft.Container(reference, col={"xs": 12, "md": 4}),
                    ]
                ),
                allocation_mode,
                manual_column,
                notes,
            ],
            on_close=close, on_save=save,
        )
        self.page.open(sheet)
        refresh_manual()

    def confirm_delete_voucher(self, voucher_id: int) -> None:
        dialog = ft.AlertDialog(modal=True)

        def close(_=None):
            self.page.close(dialog)

        def delete(_):
            try:
                self.ctx.payments.delete_voucher(voucher_id)
                self.page.close(dialog)
                self.notify("تم حذف السند وعكس أثره المالي", kind="success", sound_kind="delete")
                self.show_vouchers()
                self._changed()
            except Exception as exc:
                self.page.close(dialog)
                self.notify(str(exc), kind="error")

        dialog.title = ft.Text("حذف السند")
        dialog.content = ft.Text("سيتم حذف السند وتوزيعاته وإعادة احتساب الذمم والفواتير. هل تريد المتابعة؟")
        dialog.actions = [ft.TextButton("إلغاء", on_click=close), ft.FilledButton("حذف", icon=ft.Icons.DELETE_FOREVER, on_click=delete)]
        self.page.open(dialog)

    def show_expenses(self) -> None:
        self._set_header("المصروفات", "تسجيل ومتابعة مصروفات النشاط")
        search = SelectAllTextField(
            label="بحث في المصروفات",
            hint_text="البيان، التصنيف أو المرجع",
            prefix_icon=ft.Icons.SEARCH,
            on_focus=lambda e: grid.disarm(),
        )
        summary = ft.ResponsiveRow(spacing=8, run_spacing=8)
        summary_toggle = self._summary_toggle(summary)
        filter_state = {"month_only": False}
        # Same client-visible paging as parties/items/invoices/vouchers —
        # see parties_view.py.
        render_limit = {"n": 60}

        # ---- desktop grid (same spreadsheet treatment as items/invoices) --
        grid_sort = {"key": None, "dir": None}
        density_state = {"compact": self.ctx.settings.get("ui.expenses_density", "comfortable") == "compact"}
        selected_state = {"rows": []}
        export_state = {"rows": []}
        columns_visibility = ColumnVisibility(
            columns=EXPENSE_GRID_COLUMNS,
            settings=self.ctx.settings,
            preference_key="ui.expenses_hidden_columns",
            auto_hidden={"reference"} if (self.page.width or 1200) < 720 else set(),
            on_change=lambda: (
                grid.set_hidden_keys(columns_visibility.hidden_keys),
                self.page.update(),
            ),
        )
        columns_button = columns_visibility.button()

        async def export_csv(_=None) -> None:
            rows_out = export_state["rows"]
            if not rows_out:
                self.notify("لا توجد بيانات للتصدير", kind="warning")
                return
            if self.native_files is None:
                self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                path = Path(tempfile.gettempdir()) / f"nano_expenses_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = _csv.writer(fh)
                    writer.writerow(["البيان", "التصنيف", "التاريخ", "المرجع", "إيصال", "المبلغ"])
                    for exp in rows_out:
                        writer.writerow([
                            exp.get("description") or "",
                            exp.get("category_name") or "بلا تصنيف",
                            exp.get("expense_date") or "",
                            exp.get("reference") or "",
                            "نعم" if exp.get("has_receipt") else "لا",
                            exp.get("amount") or 0,
                        ])
                await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير المصروفات")
                self.notify("تم تجهيز ملف CSV (بترميز يفتح في Excel العربي)")
            except Exception as exc:
                self.notify(str(exc), kind="error")

        export_button = ft.IconButton(
            icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
            tooltip="تصدير CSV للعرض الحالي",
            on_click=export_csv,
        )

        def _grid_formatter(col, value, row):
            if col.key == "category_name":
                return str(value or "بلا تصنيف")
            if col.key == "_receipt":
                if row.get("has_receipt"):
                    return ("إيصال", Colors.PRIMARY, Colors.PRIMARY_BG)
                return ("—", Colors.TEXT_SECONDARY, Colors.BACKGROUND)
            if col.key == "reference":
                return str(value or "بدون مرجع")
            if col.kind == "money":
                return self.money(value)
            return None

        def expense_expand_panel(row: dict) -> ft.Control:
            return ft.Row(
                [
                    _detail_chip(ft.Icons.CATEGORY_OUTLINED, "التصنيف", str(row.get("category_name") or "بلا تصنيف")),
                    _detail_chip(ft.Icons.CALENDAR_MONTH_OUTLINED, "التاريخ", str(row.get("expense_date") or "—")),
                    _detail_chip(ft.Icons.TAG_OUTLINED, "المرجع", str(row.get("reference") or "بدون مرجع")),
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

        def render_bulk_bar(rows_out: list[dict]) -> None:
            selected_state["rows"] = list(rows_out)
            if not rows_out:
                bulk_bar.visible = False
                bulk_bar.content = None
            else:
                bulk_bar.content = ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECK_BOX, size=16, color=Colors.PRIMARY),
                        ft.Text(f"تم تحديد {len(rows_out)}", size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK),
                        ft.Container(expand=True),
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
            chosen = list(selected_state["rows"])
            if not chosen:
                return
            dialog = ft.AlertDialog(modal=True, title=ft.Text("تأكيد حذف المصروفات"))

            def yes(_ev=None) -> None:
                self.page.close(dialog)
                deleted = 0
                failures = 0
                for exp in chosen:
                    try:
                        self.ctx.expenses.delete_expense(int(exp.get("id") or 0))
                        deleted += 1
                    except Exception:
                        failures += 1
                if failures:
                    self.notify(f"حُذف {deleted}، وتعذّر حذف {failures}", kind="warning")
                else:
                    self.notify(f"تم حذف {deleted} مصروف", sound_kind="success")
                self.show_expenses()
                self._changed()

            def no(_ev=None) -> None:
                self.page.close(dialog)

            dialog.content = ft.Text(f"سيتم حذف {len(chosen)} مصروف. هل تريد المتابعة؟")
            dialog.actions = [
                ft.TextButton("إلغاء", on_click=no),
                ft.FilledButton("حذف", on_click=yes, style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE)),
            ]
            self.page.open(dialog)

        def toggle_density(_=None) -> None:
            compact = not density_state["compact"]
            density_state["compact"] = compact
            grid.set_row_height(36 if compact else 48)
            density_button.icon = ft.Icons.DENSITY_SMALL if compact else ft.Icons.DENSITY_MEDIUM
            density_button.tooltip = "عرض مريح" if compact else "عرض مضغوط"
            self.ctx.settings.set("ui.expenses_density", "compact" if compact else "comfortable")
            self.page.update()

        density_button = ft.IconButton(
            icon=ft.Icons.DENSITY_SMALL if density_state["compact"] else ft.Icons.DENSITY_MEDIUM,
            tooltip="عرض مريح" if density_state["compact"] else "عرض مضغوط",
            on_click=toggle_density,
        )
        bulk_bar = ft.Container(visible=False)

        def expense_menu(row: dict) -> list:
            expense_id = int(row.get("id") or 0)
            items: list = [("تعديل", ft.Icons.EDIT_OUTLINED, lambda i=expense_id: self.show_expense_dialog(i))]
            if row.get("has_receipt"):
                items.append(("عرض الإيصال", ft.Icons.IMAGE_OUTLINED, lambda i=expense_id: self.show_receipt_photo(i)))
            items.append(None)
            items.append(("حذف", ft.Icons.DELETE_OUTLINE, lambda i=expense_id: self.confirm_delete_expense(i)))
            return items

        grid_height = max(300, min(560, int((self.page.height or 760) * 0.52)))
        grid = DataGrid(
            columns=EXPENSE_GRID_COLUMNS,
            rows=[],
            page=self.page,
            row_height=36 if density_state["compact"] else 48,
            viewport_height=grid_height,
            hidden_keys=columns_visibility.hidden_keys,
            on_row_activate=lambda row: self.show_expense_dialog(int(row.get("id") or 0)),
            on_sort=apply_grid_sort,
            selectable=True,
            on_selection_change=render_bulk_bar,
            on_row_menu=expense_menu,
            on_row_expand=expense_expand_panel,
            on_search_shortcut=focus_search,
            on_new_shortcut=lambda: self.show_expense_dialog(),
            formatter=_grid_formatter,
            boxed=True,
            center_cells=True,
            activate_tooltip="تعديل المصروف",
            empty_text="لا توجد مصروفات",
        )
        grid_more = ft.Container(visible=False)
        desktop_list = ft.Column([bulk_bar, ft.Container(grid), grid_more], spacing=8)
        list_switch = ft.Container(content=desktop_list)

        def toggle_month(_=None):
            filter_state["month_only"] = not filter_state["month_only"]
            render_limit["n"] = 60
            refresh()

        def refresh(_=None):
            expenses = self.ctx.expenses.list_expenses()
            q = (search.value or "").strip().casefold()
            current_month = date.today().isoformat()[:7]
            filtered = [
                e for e in expenses
                if (not q or q in f"{e.get('description','')} {e.get('category_name','')} {e.get('reference','')}".casefold())
                and (not filter_state["month_only"] or str(e.get("expense_date") or "").startswith(current_month))
            ]
            total = sum(float(e.get("amount") or 0) for e in expenses)
            categories = {str(e.get("category_name") or "بلا تصنيف") for e in expenses}
            month_total = sum(float(e.get("amount") or 0) for e in expenses if str(e.get("expense_date") or "").startswith(current_month))
            summary.controls = metric_controls([
                kpi_card("إجمالي المصروفات", "", ft.Icons.PAYMENTS_OUTLINED, Colors.DANGER_DARK, amount_usd=total, settings=self.ctx.settings),
                kpi_card("هذا الشهر", "", ft.Icons.CALENDAR_MONTH_OUTLINED, Colors.ORANGE, on_tap=lambda _: toggle_month(), amount_usd=month_total, settings=self.ctx.settings),
                kpi_card("التصنيفات", str(len(categories)), ft.Icons.CATEGORY_OUTLINED, Colors.PURPLE),
            ])
            if grid_sort["key"]:
                sort_key = grid_sort["key"]
                reverse = grid_sort["dir"] == "desc"

                def sort_value(expense):
                    if sort_key == "amount":
                        try:
                            return float(expense.get(sort_key) or 0)
                        except (TypeError, ValueError):
                            return 0.0
                    return str(expense.get(sort_key) or "")

                filtered.sort(key=sort_value, reverse=reverse)
            export_state["rows"] = filtered
            grid.set_hidden_keys(columns_visibility.hidden_keys)
            if not filtered:
                grid.set_rows([])
                if expenses:
                    list_switch.content = empty_state(
                        "لا توجد مصروفات مطابقة",
                        icon=ft.Icons.RECEIPT_OUTLINED,
                        hint="جرّب تغيير البحث أو عوامل التصفية",
                    )
                else:
                    list_switch.content = empty_state(
                        "لا توجد مصروفات بعد",
                        icon=ft.Icons.RECEIPT_OUTLINED,
                        hint="سجّل أول مصروف لمتابعة تكاليف النشاط",
                        action_label="مصروف جديد",
                        on_action=lambda _: self.show_expense_dialog(),
                    )
                self.page.update()
                return
            shown = filtered[: render_limit["n"]]
            grid.set_rows(shown)
            remaining = len(filtered) - len(shown)
            if remaining > 0:
                grid_more.content = ft.Row(
                    [ft.OutlinedButton(
                        f"تحميل المزيد ({remaining})", icon=ft.Icons.EXPAND_MORE_ROUNDED,
                        on_click=lambda _=None: (render_limit.__setitem__("n", render_limit["n"] + 60), refresh())),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                )
                grid_more.visible = True
            else:
                grid_more.visible = False
            list_switch.content = desktop_list
            self.page.update()

        def refresh_from_search(_=None):
            render_limit["n"] = 60
            refresh()

        search.on_change = refresh_from_search
        # Same sticky-header / scrolling-body split as show_vouchers and
        # invoice_view: section tabs + search stay pinned, KPI cards and the
        # expense list scroll together beneath them.
        sticky_header = ft.Container(
            ft.Column(
                [
                    self._section_nav("expenses"),
                    ft.Row(
                        [ft.Container(search, expand=True), columns_button, export_button, density_button],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row([summary_toggle], alignment=ft.MainAxisAlignment.END),
                ],
                spacing=10,
            ),
            padding=ft.padding.only(left=16, right=16, top=14, bottom=12),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        scroll_body = ft.Container(
            ft.Column(
                [
                    summary,
                    ft.Row([ft.FilledButton("مصروف جديد", icon=ft.Icons.ADD, on_click=lambda _: self.show_expense_dialog())]),
                    list_switch,
                    ft.Container(height=24),
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            expand=True,
            padding=ft.padding.only(top=12, left=16, right=16),
        )
        self.content.content = ft.Column([sticky_header, scroll_body], spacing=0, expand=True)
        refresh()

    def show_expense_dialog(self, expense_id: int | None = None) -> None:
        existing = self.ctx.expenses.get_expense(expense_id) if expense_id else None
        # PHASE10 wave2 (B4): optional receipt photo captured with the system
        # camera (see native_files.capture_receipt). Bytes stay in memory here
        # and are written straight into the expense row's BLOB column on save
        # -- no temp file on the device, and it travels with every backup.
        import base64 as _b64

        _stored = self.ctx.expenses.get_expense_receipt(expense_id) if expense_id else None
        receipt_photo: dict = {
            "bytes": (_stored["image"] if _stored else None),
            "name": (_stored.get("name") if _stored else None),
        }

        def _photo_preview_controls() -> list:
            if receipt_photo["bytes"]:
                return [
                    ft.Container(
                        ft.Image(src_base64=_b64.b64encode(receipt_photo["bytes"]).decode("ascii"), height=130, fit=ft.ImageFit.COVER),
                        border_radius=12, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    ),
                    ft.Text(receipt_photo["name"] or "receipt.jpg", size=11, color=Colors.TEXT_SECONDARY),
                ]
            return [ft.Text("لا توجد صورة إيصال — يمكنك التقاطها من زر التصوير", size=11, color=Colors.TEXT_FAINT)]

        photo_area = ft.Column(_photo_preview_controls(), spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

        async def _capture_receipt(_=None):
            if self.native_files is None:
                self.notify("صورة الإيصال غير مدعومة في هذا البناء")
                return
            try:
                shot = await self.native_files.capture_receipt(filename="receipt.jpg")
            except (RuntimeError, OSError) as exc:
                self.notify(str(exc), kind="error")
                return
            if not shot:
                return  # المستخدم تراجع عن التصوير — ليس خطأ
            receipt_photo["bytes"] = _b64.b64decode(shot["bytes"])
            receipt_photo["name"] = shot.get("name") or "receipt.jpg"
            photo_area.controls = _photo_preview_controls()
            self.page.update()
            self.notify("تم التقاط صورة الإيصال", kind="success")

        def _remove_receipt(_=None):
            receipt_photo["bytes"] = None
            receipt_photo["name"] = None
            photo_area.controls = _photo_preview_controls()
            self.page.update()

        capture_fallback = feature_status("camera_capture") == "fallback:file-pick"
        capture_button = ft.OutlinedButton(
            "اختيار صورة الإيصال" if capture_fallback else "التقاط صورة الإيصال",
            icon=ft.Icons.IMAGE_OUTLINED if capture_fallback else ft.Icons.PHOTO_CAMERA_OUTLINED,
            on_click=lambda _: self.page.run_task(_capture_receipt),
        )
        remove_button = ft.TextButton(
            "إزالة الصورة", icon=ft.Icons.DELETE_OUTLINE, on_click=_remove_receipt,
        )
        photo_buttons = [capture_button] + ([remove_button] if receipt_photo["bytes"] else [])
        categories = self.ctx.expenses.list_categories()
        category = SearchSelect(
            label="التصنيف",
            value=str(existing["category_id"]) if existing and existing.get("category_id") else None,
            choices=[(str(c["id"]), c["name"]) for c in categories],
        )
        new_category = SelectAllTextField(label="تصنيف جديد")
        description = SelectAllTextField(label="البيان", value=(existing["description"] if existing else ""))
        amount = SmartAmountField(
            label=currency.amount_field_label("المبلغ", self.ctx.settings),
            value=currency.to_input_text(existing.get("amount", 0) if existing else 0, self.ctx.settings),
        )
        edate = SmartDateField(label="التاريخ", value=str(existing["expense_date"] if existing else date.today().isoformat()))
        reference = SelectAllTextField(label="المرجع", value=(existing.get("reference") or "") if existing else "")
        notes = SelectAllTextField(label="ملاحظات", value=(existing.get("notes") or "") if existing else "", multiline=True)
        sheet = new_form_sheet()

        def close(_=None):
            self.page.close(sheet)

        def save(_):
            try:
                category_id = int(category.value) if category.value else None
                if (new_category.value or "").strip():
                    category_id = self.ctx.expenses.create_category(new_category.value)
                kwargs = {
                    "amount": currency.parse_display_input(amount.value, self.ctx.settings),
                    "description": description.value or "",
                    "expense_date": edate.value,
                    "category_id": category_id,
                    "reference": reference.value,
                    "notes": notes.value,
                    "receipt_image": receipt_photo["bytes"],
                    "receipt_name": receipt_photo["name"],
                }
                if expense_id:
                    self.ctx.expenses.update_expense(expense_id, **kwargs)
                else:
                    self.ctx.expenses.create_expense(**kwargs)
                close()
                self.notify("تم حفظ المصروف")
                self.show_expenses()
                self._changed()
            except Exception as exc:
                self.notify(str(exc), kind="error")

        render_form_sheet(
            self.page, sheet,
            title="تعديل مصروف" if expense_id else "مصروف جديد",
            fields=[category, new_category, description, ft.Row([amount, edate], wrap=True), reference, notes,
                    ft.Column([photo_area, ft.Row(photo_buttons)], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER)],
            on_close=close, on_save=save,
        )
        self.page.open(sheet)

    def show_receipt_photo(self, expense_id: int) -> None:
        receipt = self.ctx.expenses.get_expense_receipt(expense_id)
        if not receipt or not receipt.get("image"):
            self.notify("لا توجد صورة إيصال لهذا المصروف")
            return
        import base64 as _b64

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(receipt.get("name") or "صورة الإيصال", text_align=ft.TextAlign.CENTER),
            content=ft.Container(
                ft.Image(src_base64=_b64.b64encode(receipt["image"]).decode("ascii"), fit=ft.ImageFit.CONTAIN),
                border_radius=12, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ),
            actions=[ft.FilledButton("إغلاق", on_click=lambda _: self.page.close(dialog))],
            actions_alignment=ft.MainAxisAlignment.CENTER,
        )
        self.page.open(dialog)

    def confirm_delete_expense(self, expense_id: int) -> None:
        dialog = ft.AlertDialog(modal=True)

        def close(_=None):
            self.page.close(dialog)

        def delete(_):
            try:
                self.ctx.expenses.delete_expense(expense_id)
                self.page.close(dialog)
                self.notify("تم حذف المصروف", kind="success", sound_kind="delete")
                self.show_expenses()
                self._changed()
            except Exception as exc:
                self.page.close(dialog)
                self.notify(str(exc), kind="error")

        dialog.title = ft.Text("حذف المصروف")
        dialog.content = ft.Text("سيتم حذف المصروف وعكس أثره على الصندوق والأرباح.")
        dialog.actions = [ft.TextButton("إلغاء", on_click=close), ft.FilledButton("حذف", on_click=delete)]
        self.page.open(dialog)

    def show_statements(self, party_type: str, party_id: int | None = None) -> None:
        source = self.ctx.customers.list() if party_type == "customer" else self.ctx.suppliers.list()
        title = "كشف حساب العملاء" if party_type == "customer" else "كشف حساب الموردين"
        active = "customers" if party_type == "customer" else "suppliers"
        self._set_header(title, "الرصيد والحركات على الحساب خلال فترة محددة")
        party_dd = SearchSelect(
            label="العميل" if party_type == "customer" else "المورد",
            choices=[(str(p["id"]), p["name"]) for p in source],
        )
        date_from = SmartDateField(label="من تاريخ", hint_text="YYYY-MM-DD")
        date_to = SmartDateField(label="إلى تاريخ", hint_text="YYYY-MM-DD")
        result = ft.Column(spacing=8)
        export_actions = ft.Row(visible=False, wrap=True)
        # Same client-visible paging as parties/items/invoices/vouchers/
        # expenses — see parties_view.py. A long-lived account's full
        # history can run to hundreds of movements; render a page of the
        # (already fully computed, running-balance) rows at a time. Print
        # and PDF export are untouched — they always use the complete
        # `data["rows"]` regardless of what's currently rendered on screen.
        render_limit = {"n": 60}

        async def print_statement(_):
            if not party_dd.value:
                self.notify("اختر الحساب أولًا")
                return
            if self.native_files is None:
                self.notify("الطباعة الأصلية غير مهيأة في هذا البناء")
                return
            async def print_selected(layout: str, width: int) -> None:
                try:
                    html = self.ctx.documents.statement_html(
                        party_type,
                        int(party_dd.value),
                        date_from=(date_from.value or "").strip() or None,
                        date_to=(date_to.value or "").strip() or None,
                    )
                    html = apply_print_profile(html, layout, width)
                    await self.native_files.print_html(
                        html,
                        name=f"nano-statement-{party_type}-{party_dd.value}",
                        paper_layout=layout,
                        roll_width_mm=width,
                    )
                except Exception as exc:
                    self.notify(str(exc), kind="error")

            open_print_profile_dialog(self.page, self.ctx.settings, print_selected)

        async def share_statement_pdf(_):
            if not party_dd.value:
                self.notify("اختر الحساب أولًا")
                return
            if self.native_files is None:
                self.notify("تصدير PDF غير مهيأ في هذا البناء")
                return
            async def share_selected(layout: str, width: int) -> None:
                try:
                    html = self.ctx.documents.statement_html(
                        party_type,
                        int(party_dd.value),
                        date_from=(date_from.value or "").strip() or None,
                        date_to=(date_to.value or "").strip() or None,
                    )
                    html = apply_print_profile(html, layout, width)
                    await self.native_files.share_pdf(
                        html,
                        filename=f"nano_statement_{party_type}_{party_dd.value}.pdf",
                        paper_layout=layout,
                        roll_width_mm=width,
                    )
                except Exception as exc:
                    self.notify(str(exc), kind="error")

            open_print_profile_dialog(
                self.page, self.ctx.settings, share_selected, title="تنسيق كشف PDF"
            )

        export_actions.controls = [
            ft.OutlinedButton("طباعة الكشف", icon=ft.Icons.PRINT_OUTLINED, on_click=print_statement),
            ft.OutlinedButton("مشاركة PDF", icon=ft.Icons.PICTURE_AS_PDF_OUTLINED, on_click=share_statement_pdf),
        ]

        def render(_=None):
            result.controls = []
            if not party_dd.value:
                export_actions.visible = False
                self.page.update()
                return
            try:
                data = self.ctx.statements.party_statement(
                    party_type,
                    int(party_dd.value),
                    date_from=(date_from.value or "").strip() or None,
                    date_to=(date_to.value or "").strip() or None,
                )
                export_actions.visible = True
                balance = float(data["current_balance"])
                balance_label = "رصيد مستحق" if balance >= 0 else "رصيد دائن للطرف"
                result.controls.append(
                    metric_row([
                        kpi_card("الرصيد الحالي", "", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.PRIMARY if balance >= 0 else Colors.SUCCESS, amount_usd=abs(balance), settings=self.ctx.settings, suffix=balance_label),
                        kpi_card("الرصيد الافتتاحي للفترة", "", ft.Icons.HISTORY_OUTLINED, Colors.PURPLE, amount_usd=data["opening_balance"], settings=self.ctx.settings),
                        kpi_card("فواتير مفتوحة", str(len(data["open_invoices"])), ft.Icons.RECEIPT_LONG_OUTLINED, Colors.ORANGE),
                    ])
                )
                # Same icon-badge card language as the vouchers/invoices
                # list rows -- an inflow is styled like a "قبض" row, an
                # outflow like a "دفع" row, instead of a plain bordered line.
                for row in data["rows"][: render_limit["n"]]:
                    movement = float(row["movement"])
                    inflow = movement >= 0
                    accent = Colors.SUCCESS if inflow else Colors.DANGER_DARK
                    # Drill-down like the items/invoices lists: tapping a
                    # movement opens its source document (invoice or
                    # voucher). Other source types stay plain rows.
                    source_type = str(row.get("source_type") or "")
                    try:
                        source_id = int(row["source_id"]) if row.get("source_id") is not None else None
                    except (TypeError, ValueError):
                        source_id = None
                    if source_type == "invoice" and source_id is not None:
                        def _open(_=None, _iid=source_id):
                            if self.on_open_invoice is not None:
                                self.on_open_invoice(_iid)
                            else:
                                self.notify("عرض الفواتير غير متاح من هنا")
                        tap, chevron = _open, ft.Icon(ft.Icons.CHEVRON_LEFT, size=18, color=Colors.TEXT_FAINT)
                    elif source_type == "payment" and source_id is not None:
                        tap, chevron = (lambda _, _pid=source_id: self._open_payment_source(_pid)), ft.Icon(ft.Icons.CHEVRON_LEFT, size=18, color=Colors.TEXT_FAINT)
                    else:
                        tap, chevron = None, ft.Container(width=0)
                    result.controls.append(
                        ft.Container(
                            ft.Row(
                                [
                                    ft.Container(ft.Icon(ft.Icons.SOUTH_WEST if inflow else ft.Icons.NORTH_EAST, size=18, color=accent), width=44, height=44, alignment=ft.alignment.center, bgcolor=Colors.SUCCESS_BG if inflow else Colors.DANGER_BG, border_radius=14),
                                    ft.Column(
                                        [
                                            ft.Text(row["description"] or row["source_label"], size=13, weight=ft.FontWeight.BOLD),
                                            ft.Text(f"{row['entry_date']} • {row['source_label']} #{row['source_id'] or '—'}", size=10, color=Colors.TEXT_SECONDARY),
                                        ],
                                        expand=True,
                                        spacing=2,
                                    ),
                                    ft.Column(
                                        [
                                            money_row(abs(movement), self.ctx.settings, size=15, weight=ft.FontWeight.BOLD, color=accent, prefix="+" if inflow else "-"),
                                            ft.Row(
                                                [
                                                    ft.Text("الرصيد ", size=9, color=Colors.TEXT_FAINT),
                                                    money_row(row["balance"], self.ctx.settings, size=9, color=Colors.TEXT_FAINT),
                                                ],
                                                spacing=2, tight=True,
                                            ),
                                        ],
                                        spacing=1, horizontal_alignment=ft.CrossAxisAlignment.END,
                                    ),
                                    chevron,
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=12, bgcolor=Colors.WHITE, border=ft.border.all(1, Colors.BORDER), border_radius=16, shadow=Shadow.SM,
                            on_click=tap, ink=tap is not None,
                        )
                    )
                if not data["rows"]:
                    result.controls.append(empty_state(
                        "لا توجد حركات في الفترة المحددة",
                        icon=ft.Icons.SWAP_VERT_ROUNDED,
                        hint="جرّب توسيع نطاق التاريخ",
                    ))
                elif len(data["rows"]) > render_limit["n"]:
                    remaining = len(data["rows"]) - render_limit["n"]

                    def load_more(_=None):
                        render_limit["n"] += 60
                        render()

                    result.controls.append(
                        ft.OutlinedButton(
                            f"تحميل المزيد ({remaining})",
                            icon=ft.Icons.EXPAND_MORE_ROUNDED,
                            on_click=load_more,
                        )
                    )
            except Exception as exc:
                export_actions.visible = False
                self.notify(str(exc), kind="error")
            self.page.update()

        def render_from_filters(_=None):
            # A new account or date range is a fresh query, not a
            # continuation of whatever page the previous account had
            # scrolled to — always start back at the first page.
            render_limit["n"] = 60
            render()

        party_dd.on_change = render_from_filters
        # Same sticky-header / scrolling-body split as the other finance
        # sections: section tabs and the account/date filters stay pinned,
        # the KPI cards and statement rows scroll beneath them.
        sticky_header = ft.Container(
            ft.Column(
                [
                    self._section_nav(active),
                    ft.ResponsiveRow(
                        [
                            ft.Container(party_dd, col={"xs": 12, "md": 6}),
                            ft.Container(date_from, col={"xs": 6, "md": 3}),
                            ft.Container(date_to, col={"xs": 6, "md": 3}),
                        ]
                    ),
                    ft.Row([ft.FilledButton("عرض الكشف", icon=ft.Icons.SEARCH, on_click=render_from_filters), export_actions], wrap=True),
                ],
                spacing=10,
            ),
            padding=ft.padding.only(left=16, right=16, top=14, bottom=12),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        scroll_body = ft.Container(
            ft.Column([result, ft.Container(height=24)], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True),
            expand=True,
            padding=ft.padding.only(top=12, left=16, right=16),
        )
        self.content.content = ft.Column([sticky_header, scroll_body], spacing=0, expand=True)
        if party_id is not None:
            # Deep-link entry (party card, receivables report): preselect
            # the account and render immediately instead of waiting for a
            # manual pick. Unknown ids simply leave the picker empty.
            try:
                wanted = str(int(party_id))
            except (TypeError, ValueError):
                wanted = None
            if wanted is not None and any(str(p["id"]) == wanted for p in source):
                party_dd.value = wanted
                render_from_filters()
                return
        self.page.update()

    def show_ledger(self) -> None:
        """General-ledger hub inside Finance (option A): trial balance over
        every posted account plus a per-account chronological statement --
        the same card language as the customer/supplier statements, so the
        whole money area reads as one system."""
        self._set_header("دفتر الأستاذ", "ميزان المراجعة وكشوف الحسابات من القيود المرحلة")
        account_dd = SearchSelect(
            label="الحساب",
            choices=[(code, label) for code, (label, _) in StatementService.ACCOUNT_LABELS.items()],
        )
        date_from = SmartDateField(label="من تاريخ", hint_text="YYYY-MM-DD")
        date_to = SmartDateField(label="إلى تاريخ", hint_text="YYYY-MM-DD")
        result = ft.Column(spacing=8)
        render_limit = {"n": 60}

        def render_trial() -> None:
            tb = self.ctx.statements.trial_balance()
            balanced = bool(tb["balanced"])
            result.controls.append(
                ft.Text("ميزان المراجعة", size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
            )
            result.controls.append(
                metric_row([
                    kpi_card("إجمالي مدين", "", ft.Icons.ARROW_DOWNWARD, Colors.PRIMARY, amount_usd=tb["total_debit"], settings=self.ctx.settings),
                    kpi_card("إجمالي دائن", "", ft.Icons.ARROW_UPWARD, Colors.PURPLE, amount_usd=tb["total_credit"], settings=self.ctx.settings),
                    kpi_card("حالة التوازن", "", ft.Icons.CHECK_CIRCLE_OUTLINE if balanced else ft.Icons.WARNING_AMBER_ROUNDED, Colors.SUCCESS if balanced else Colors.DANGER_DARK, amount_usd=abs(tb["difference"]), settings=self.ctx.settings, suffix="متوازن" if balanced else "فرق غير مرحل"),
                ], spacing=8)
            )

            def pick(code: str):
                def handler(_=None):
                    account_dd.value = code
                    try:
                        account_dd.update()
                    except Exception:
                        pass
                    render_from_filters()
                return handler

            for acc in tb["accounts"]:
                has_activity = abs(float(acc["debit"]) or 0) > 1e-9 or abs(float(acc["credit"]) or 0) > 1e-9
                result.controls.append(
                    ft.Container(
                        ft.Row(
                            [
                                ft.Container(
                                    ft.Text(str(acc["code"]), size=10, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY),
                                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                                    bgcolor=Colors.PRIMARY_BG, border_radius=8,
                                ),
                                ft.Column(
                                    [
                                        ft.Text(acc["label"], size=13, weight=ft.FontWeight.BOLD, expand=True),
                                        ft.Row(
                                            [
                                                ft.Text("مدين ", size=9, color=Colors.TEXT_FAINT),
                                                money_row(acc["debit"], self.ctx.settings, size=9, color=Colors.TEXT_FAINT),
                                                ft.Text(" • دائن ", size=9, color=Colors.TEXT_FAINT),
                                                money_row(acc["credit"], self.ctx.settings, size=9, color=Colors.TEXT_FAINT),
                                            ],
                                            spacing=2, tight=True,
                                        ),
                                    ],
                                    expand=True, spacing=2,
                                ),
                                money_row(acc["balance"], self.ctx.settings, size=14, weight=ft.FontWeight.BOLD),
                                ft.Icon(ft.Icons.CHEVRON_LEFT, size=18, color=Colors.TEXT_FAINT),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=11, bgcolor=Colors.WHITE if has_activity else Colors.BACKGROUND_ALT,
                        border=ft.border.all(1, Colors.BORDER), border_radius=16, shadow=Shadow.SM,
                        on_click=pick(str(acc["code"])), ink=True,
                    )
                )

        def render_account() -> None:
            data = self.ctx.statements.account_statement(
                str(account_dd.value),
                date_from=(date_from.value or "").strip() or None,
                date_to=(date_to.value or "").strip() or None,
            )
            result.controls.append(
                ft.Text(f"كشف حساب: {data['label']}", size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
            )
            result.controls.append(
                metric_row([
                    kpi_card("الرصيد الافتتاحي", "", ft.Icons.HISTORY_OUTLINED, Colors.PURPLE, amount_usd=data["opening_balance"], settings=self.ctx.settings),
                    kpi_card("إجمالي مدين", "", ft.Icons.ARROW_DOWNWARD, Colors.PRIMARY, amount_usd=data["debit_total"], settings=self.ctx.settings),
                    kpi_card("إجمالي دائن", "", ft.Icons.ARROW_UPWARD, Colors.PURPLE, amount_usd=data["credit_total"], settings=self.ctx.settings),
                    kpi_card("الرصيد الختامي", "", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.SUCCESS, amount_usd=data["closing_balance"], settings=self.ctx.settings),
                ], spacing=8)
            )
            for row in data["rows"][: render_limit["n"]]:
                movement = float(row["movement"])
                inflow = movement >= 0
                accent = Colors.SUCCESS if inflow else Colors.DANGER_DARK
                source_type = str(row.get("source_type") or "")
                try:
                    source_id = int(row["source_id"]) if row.get("source_id") is not None else None
                except (TypeError, ValueError):
                    source_id = None
                if source_type == "invoice" and source_id is not None:
                    def _open(_=None, _iid=source_id):
                        if self.on_open_invoice is not None:
                            self.on_open_invoice(_iid)
                        else:
                            self.notify("عرض الفواتير غير متاح من هنا")
                    tap, chevron = _open, ft.Icon(ft.Icons.CHEVRON_LEFT, size=18, color=Colors.TEXT_FAINT)
                elif source_type == "payment" and source_id is not None:
                    tap, chevron = (lambda _, _pid=source_id: self._open_payment_source(_pid)), ft.Icon(ft.Icons.CHEVRON_LEFT, size=18, color=Colors.TEXT_FAINT)
                else:
                    tap, chevron = None, ft.Container(width=0)
                party = row.get("party_name") or "—"
                result.controls.append(
                    ft.Container(
                        ft.Row(
                            [
                                ft.Container(ft.Icon(ft.Icons.SOUTH_WEST if inflow else ft.Icons.NORTH_EAST, size=18, color=accent), width=44, height=44, alignment=ft.alignment.center, bgcolor=Colors.SUCCESS_BG if inflow else Colors.DANGER_BG, border_radius=14),
                                ft.Column(
                                    [
                                        ft.Text(f"{row['description'] or row['source_label']} • {party}", size=13, weight=ft.FontWeight.BOLD),
                                        ft.Text(f"{row['entry_date']} • {row['source_label']} #{row['source_id'] or '—'}", size=10, color=Colors.TEXT_SECONDARY),
                                    ],
                                    expand=True, spacing=2,
                                ),
                                ft.Column(
                                    [
                                        money_row(abs(movement), self.ctx.settings, size=15, weight=ft.FontWeight.BOLD, color=accent, prefix="+" if inflow else "-"),
                                        ft.Row(
                                            [
                                                ft.Text("الرصيد ", size=9, color=Colors.TEXT_FAINT),
                                                money_row(row["balance"], self.ctx.settings, size=9, color=Colors.TEXT_FAINT),
                                            ],
                                            spacing=2, tight=True,
                                        ),
                                    ],
                                    spacing=1, horizontal_alignment=ft.CrossAxisAlignment.END,
                                ),
                                chevron,
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=12, bgcolor=Colors.WHITE, border=ft.border.all(1, Colors.BORDER), border_radius=16, shadow=Shadow.SM,
                        on_click=tap, ink=tap is not None,
                    )
                )
            if not data["rows"]:
                result.controls.append(empty_state(
                    "لا توجد حركات لهذا الحساب في الفترة المحددة",
                    icon=ft.Icons.DESCRIPTION_OUTLINED,
                    hint="جرّب توسيع نطاق التاريخ",
                ))
            elif len(data["rows"]) > render_limit["n"]:
                remaining = len(data["rows"]) - render_limit["n"]

                def load_more(_=None):
                    render_limit["n"] += 60
                    render()

                result.controls.append(
                    ft.OutlinedButton(
                        f"تحميل المزيد ({remaining})",
                        icon=ft.Icons.EXPAND_MORE_ROUNDED,
                        on_click=load_more,
                    )
                )

        def render(_=None):
            result.controls = []
            try:
                render_trial()
                if account_dd.value:
                    render_account()
            except Exception as exc:
                self.notify(str(exc), kind="error")
            self.page.update()

        def render_from_filters(_=None):
            render_limit["n"] = 60
            render()

        account_dd.on_change = render_from_filters
        sticky_header = ft.Container(
            ft.Column(
                [
                    self._section_nav("ledger"),
                    ft.ResponsiveRow(
                        [
                            ft.Container(account_dd, col={"xs": 12, "md": 6}),
                            ft.Container(date_from, col={"xs": 6, "md": 3}),
                            ft.Container(date_to, col={"xs": 6, "md": 3}),
                        ]
                    ),
                    ft.Row([ft.FilledButton("عرض", icon=ft.Icons.SEARCH, on_click=render_from_filters)], wrap=True),
                ],
                spacing=10,
            ),
            padding=ft.padding.only(left=16, right=16, top=14, bottom=12),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        scroll_body = ft.Container(
            ft.Column([result, ft.Container(height=24)], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True),
            expand=True,
            padding=ft.padding.only(top=12, left=16, right=16),
        )
        self.content.content = ft.Column([sticky_header, scroll_body], spacing=0, expand=True)
        render()
