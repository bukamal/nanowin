from __future__ import annotations

import csv as _csv
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import flet as ft

from nano_offline.core.toast import toast

from nano_offline.components import SearchSelect, SelectAllTextField, SmartAmountField, SmartDateField, empty_state, kpi_card, new_form_sheet, render_form_sheet, metric_row, metric_controls, DataGrid, GridColumn
from nano_offline.components.column_visibility import ColumnVisibility
from nano_offline.components.buttons import danger_button, primary_button, secondary_button
from nano_offline.components.money_text import money_row
from nano_offline.components import sheets
from nano_offline.components.print_profile import apply_print_profile, open_print_profile_dialog
from nano_offline.services.statement_service import StatementService
from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.core import currency
from nano_offline.core import privacy
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
    GridColumn(
        "amount", "المبلغ", width=110, align="end", kind="money",
        color_role=lambda _value, row: "success" if row.get("voucher_type") == "receipt" else "danger",
        tooltip="قيمة السند الإجمالية",
    ),
    GridColumn("allocated_amount", "موزّع", width=100, align="end", kind="money", tooltip="الجزء الموزّع على فواتير"),
    GridColumn(
        "unallocated_amount", "على الحساب", width=110, align="end", kind="money",
        color_role=lambda value, _row: "warning" if float(value or 0) > 0 else None,
        tooltip="رصيد غير موزّع — الصف مُظلَّل عند وجوده",
    ),
]

# Desktop grid columns for the expenses screen.
EXPENSE_GRID_COLUMNS = [
    GridColumn("description", "البيان", tooltip="وصف المصروف — اضغط للترتيب"),
    GridColumn("category_name", "التصنيف", width=140, align="center", kind="badge", tooltip="تصنيف المصروف"),
    GridColumn("expense_date", "التاريخ", width=100, align="center", tooltip="تاريخ المصروف"),
    GridColumn("reference", "المرجع", width=130, tooltip="رقم أو مرجع المستند — يُخفى تلقائيًا على الشاشات الضيقة"),
    GridColumn("_receipt", "الإيصال", width=80, align="center", kind="badge", sortable=False, tooltip="صورة إيصال مرفقة"),
    GridColumn("amount", "المبلغ", width=120, align="end", kind="money", color_role="danger", tooltip="قيمة المصروف"),
]

PARTY_STATEMENT_GRID_COLUMNS = [
    GridColumn("entry_date", "التاريخ", width=105, align="center"),
    GridColumn("source_label", "المصدر", width=95, align="center", kind="badge"),
    GridColumn("description", "البيان"),
    GridColumn("debit", "مدين", width=115, align="end", kind="money"),
    GridColumn("credit", "دائن", width=115, align="end", kind="money"),
    GridColumn("movement", "الحركة", width=115, align="end", kind="money"),
    GridColumn("balance", "الرصيد", width=125, align="end", kind="money"),
]

LEDGER_GRID_COLUMNS = [
    GridColumn("entry_date", "التاريخ", width=105, align="center"),
    GridColumn("source_label", "المصدر", width=95, align="center", kind="badge"),
    GridColumn("party_name", "الطرف", width=170),
    GridColumn("description", "البيان"),
    GridColumn("debit", "مدين", width=115, align="end", kind="money"),
    GridColumn("credit", "دائن", width=115, align="end", kind="money"),
    GridColumn("movement", "الحركة", width=115, align="end", kind="money"),
    GridColumn("balance", "الرصيد", width=125, align="end", kind="money"),
]

TRIAL_BALANCE_GRID_COLUMNS = [
    GridColumn("code", "الحساب", width=100, align="center"),
    GridColumn("label", "اسم الحساب"),
    GridColumn("debit", "مدين", width=145, align="end", kind="money"),
    GridColumn("credit", "دائن", width=145, align="end", kind="money"),
    GridColumn("balance", "الرصيد", width=145, align="end", kind="money"),
]

COLLECTION_GRID_COLUMNS = [
    GridColumn("invoice_id", "الفاتورة", width=85, align="center", kind="number"),
    GridColumn("party_name", "العميل"),
    GridColumn("invoice_date", "تاريخ الفاتورة", width=110, align="center"),
    GridColumn("due_date", "الاستحقاق", width=110, align="center"),
    GridColumn("days_until_due", "أيام للاستحقاق", width=110, align="center", kind="number"),
    GridColumn("remaining_amount", "المتبقي", width=130, align="end", kind="money", color_role="warning"),
]

COMMITMENT_GRID_COLUMNS = [
    GridColumn("invoice_id", "الفاتورة", width=85, align="center", kind="number"),
    GridColumn("invoice_type", "النوع", width=95, align="center", kind="badge"),
    GridColumn("party_name", "الحساب"),
    GridColumn("due_date", "الاستحقاق", width=110, align="center"),
    GridColumn("days_until_due", "أيام", width=70, align="center", kind="number"),
    GridColumn("remaining_amount", "المتبقي", width=130, align="end", kind="money", color_role="warning"),
]

ANOMALY_GRID_COLUMNS = [
    GridColumn("category", "التصنيف"),
    GridColumn("current_month_to_date", "هذا الشهر", width=130, align="end", kind="money"),
    GridColumn("expected_to_date", "المتوقع", width=130, align="end", kind="money"),
    GridColumn("difference", "الزيادة", width=130, align="end", kind="money", color_role="danger"),
    GridColumn("increase_percent", "نسبة الزيادة", width=110, align="end", kind="number", color_role="danger"),
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
        self._active_finance_view: str | None = None

    @property
    def is_vouchers_view(self) -> bool:
        return self._active_finance_view == "vouchers"

    def money(self, value) -> str:
        return privacy.money_ctx(value, self.ctx)

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

    def _clear_context_actions(self) -> None:
        if self.on_context_actions:
            self.on_context_actions([])

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
            ("overview", "المؤشرات المالية", ft.Icons.QUERY_STATS_OUTLINED, self.show_overview),
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

    def show_overview(self) -> None:
        self._clear_context_actions()
        self._active_finance_view = "overview"
        self._set_header("المؤشرات المالية", "أولوية التحصيل، الالتزامات المستحقة، ومراجعة الصندوق")
        today = date.today().isoformat()
        collections = self.ctx.reports.collection_priorities(as_of=today)
        commitments = self.ctx.reports.cash_commitments(as_of=today)
        anomalies = self.ctx.reports.expense_anomalies(as_of=today)
        last_close = self.ctx.cash_day_close.last_close()
        buckets = commitments["buckets"]

        def sort_overview_grid(grid, rows: list[dict], key: str | None, direction: str | None) -> None:
            ordered = list(rows)
            if key:
                numeric = {
                    "invoice_id", "days_overdue", "days_until_due", "remaining_amount",
                    "days_until_due", "current_month_to_date",
                    "expected_to_date", "difference", "increase_percent",
                }
                ordered.sort(
                    key=lambda row: (
                        float(row.get(key) or 0) if key in numeric
                        else str(row.get(key) or "")
                    ),
                    reverse=direction == "desc",
                )
            grid.set_rows(ordered)

        summary = metric_row([
            kpi_card("ذمم العملاء", "", ft.Icons.PEOPLE_ALT_OUTLINED, Colors.PRIMARY,
                     amount_usd=collections["total_outstanding"], settings=self.ctx.settings),
            kpi_card("متأخر للتحصيل", "", ft.Icons.SCHEDULE, Colors.DANGER_DARK,
                     amount_usd=collections["overdue_total"], settings=self.ctx.settings),
            kpi_card("رصيد الصندوق", "", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.SUCCESS,
                     amount_usd=commitments["book_cash"], settings=self.ctx.settings),
            kpi_card("صافي الاستحقاق خلال 7 أيام", "", ft.Icons.EVENT_NOTE_OUTLINED,
                     Colors.WARNING_DARK, amount_usd=commitments["net_next_7_days"],
                     settings=self.ctx.settings),
            kpi_card("صافي الاستحقاق خلال 30 يومًا", "", ft.Icons.CALENDAR_MONTH_OUTLINED,
                     Colors.PURPLE, amount_usd=commitments["net_next_30_days"],
                     settings=self.ctx.settings),
            kpi_card("مصروفات تستحق المراجعة", str(len(anomalies["anomalies"])),
                     ft.Icons.TRENDING_UP, Colors.ORANGE),
        ], spacing=8)

        collection_grid = DataGrid(
            columns=COLLECTION_GRID_COLUMNS,
            rows=collections["invoices"],
            viewport_height=250,
            boxed=True,
            center_cells=True,
            on_sort=lambda key, direction: sort_overview_grid(
                collection_grid, collections["invoices"], key, direction
            ),
            empty_text="لا توجد ذمم مفتوحة",
            activate_tooltip="فتح الفاتورة",
            on_row_activate=lambda row: self.on_open_invoice(int(row["invoice_id"]))
            if self.on_open_invoice else None,
            on_row_menu=lambda row: [
                ("فتح الفاتورة", ft.Icons.OPEN_IN_NEW_ROUNDED,
                 lambda selected=row: self.on_open_invoice(int(selected["invoice_id"])))
            ] if self.on_open_invoice else [],
            row_bg=lambda row: (
                Colors.DANGER_BG if row.get("due_status") == "overdue"
                else Colors.WARNING_BG if row.get("due_status") == "due_today"
                else None
            ),
            formatter=lambda col, value, row: (
                self.money(value) if col.kind == "money"
                else "بلا تاريخ استحقاق" if col.key == "due_date" and not value
                else f"{value:+d}" if col.key == "days_until_due" and value is not None
                else str(value or "—")
            ),
        )
        commitment_grid = DataGrid(
            columns=COMMITMENT_GRID_COLUMNS,
            rows=commitments["rows"],
            viewport_height=250,
            boxed=True,
            center_cells=True,
            on_sort=lambda key, direction: sort_overview_grid(
                commitment_grid, commitments["rows"], key, direction
            ),
            empty_text="لا توجد فواتير بتاريخ استحقاق معلوم",
            activate_tooltip="فتح الفاتورة",
            on_row_activate=lambda row: self.on_open_invoice(int(row["invoice_id"]))
            if self.on_open_invoice else None,
            on_row_menu=lambda row: [
                ("فتح الفاتورة", ft.Icons.OPEN_IN_NEW_ROUNDED,
                 lambda selected=row: self.on_open_invoice(int(selected["invoice_id"])))
            ] if self.on_open_invoice else [],
            formatter=lambda col, value, row: (
                self.money(value) if col.kind == "money"
                else ("بيع — قبض متوقع" if value == "sale" else "شراء — دفع متوقع")
                if col.key == "invoice_type"
                else f"{value:+d}" if col.key == "days_until_due" and value is not None
                else str(value or "—")
            ),
            row_bg=lambda row: Colors.DANGER_BG if int(row.get("days_until_due") or 0) < 0 else None,
        )
        anomaly_grid = DataGrid(
            columns=ANOMALY_GRID_COLUMNS,
            rows=anomalies["anomalies"],
            viewport_height=210,
            boxed=True,
            center_cells=True,
            on_sort=lambda key, direction: sort_overview_grid(
                anomaly_grid, anomalies["anomalies"], key, direction
            ),
            empty_text="لا توجد زيادة غير معتادة وفق المقارنة المتاحة",
            formatter=lambda col, value, row: (
                self.money(value) if col.kind == "money"
                else f"{float(value):.0f}%" if col.key == "increase_percent" and value is not None
                else "جديد" if col.key == "increase_percent" and value is None
                else str(value or "—")
            ),
        )

        close_note = (
            f"آخر إغلاق {last_close.get('date', '—')} — الفرق عند الإغلاق "
            f"{self.money(last_close.get('variance', 0))}"
            if last_close else "لم يُسجّل إغلاق للصندوق بعد."
        )
        dated_note = (
            f"الاستحقاقات مبنية على مدد السداد المحددة لكل فاتورة. "
            f"دون تاريخ استحقاق: {commitments['undated_invoice_count']} فاتورة؛ "
            "لن تُدرج في صافي الاستحقاق."
        )

        async def export_finance_snapshot(_=None):
            if self.native_files is None:
                self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                path = Path(tempfile.gettempdir()) / f"nano_finance_overview_{datetime.now():%Y%m%d_%H%M%S}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = _csv.writer(stream)
                    writer.writerow(["البيان", "التاريخ", "الطرف/التصنيف", "الاستحقاق", "المبلغ", "ملاحظات"])
                    for row in collections["invoices"]:
                        writer.writerow([
                            "أولوية تحصيل", row.get("invoice_date"), row.get("party_name"),
                            row.get("due_date") or "بلا استحقاق", row.get("remaining_amount"),
                            row.get("due_status"),
                        ])
                    for row in commitments["rows"]:
                        writer.writerow([
                            "التزام/استحقاق", row.get("due_date"), row.get("party_name"),
                            row.get("due_date"), row.get("remaining_amount"),
                            "بيع — قبض متوقع" if row["invoice_type"] == "sale" else "شراء — دفع متوقع",
                        ])
                    for row in anomalies["anomalies"]:
                        writer.writerow([
                            "ارتفاع مصروف", today, row.get("category"), "",
                            row.get("difference"),
                            f"الحالي {row['current_month_to_date']}، المتوقع {row['expected_to_date']}",
                        ])
                await self.native_files.share_file(
                    str(path), mime_type="text/csv", subject="تصدير المؤشرات المالية"
                )
            except Exception as exc:
                self.notify(str(exc), kind="error")

        def close_cash_day(_=None):
            counted_field = SelectAllTextField(
                label=currency.amount_field_label("النقد المعدود", self.ctx.settings),
                value=currency.to_input_text(commitments["book_cash"], self.ctx.settings),
                keyboard_type=ft.KeyboardType.NUMBER,
                prefix_icon=ft.Icons.PAYMENTS_OUTLINED,
            )
            note_field = SelectAllTextField(label="ملاحظة", multiline=True, min_lines=1, max_lines=3)
            post_adjustment = ft.Switch(label="ترحيل تسوية للفرق", value=True)
            print_after = ft.Switch(label="طباعة تقرير الإغلاق بعد الحفظ", value=True)
            preview = ft.Text("", color=Colors.TEXT_SECONDARY)
            dialog = ft.AlertDialog(modal=True, title=ft.Text("مطابقة وإغلاق الصندوق"))

            def update_preview(_=None):
                try:
                    counted = currency.parse_display_input(counted_field.value, self.ctx.settings)
                    variance = counted - float(commitments["book_cash"])
                    preview.value = f"دفتر الصندوق: {self.money(commitments['book_cash'])} — الفرق: {self.money(variance)}"
                    preview.color = Colors.SUCCESS if abs(variance) < 1e-6 else Colors.WARNING_DARK
                except Exception:
                    preview.value = "أدخل مبلغًا صحيحًا"
                    preview.color = Colors.DANGER_DARK
                try:
                    preview.update()
                except Exception:
                    pass

            def save_close(_=None):
                try:
                    counted = currency.parse_display_input(counted_field.value, self.ctx.settings)
                    session = self.ctx.auth.current()
                    result = self.ctx.cash_day_close.close_day(
                        counted_cash=counted,
                        note=note_field.value or "",
                        post_adjustment=bool(post_adjustment.value),
                        username=getattr(session, "username", None),
                        user_id=getattr(session, "id", None),
                    )
                except Exception as exc:
                    self.notify(str(exc), kind="error")
                    return
                self.page.close(dialog)
                self.notify(
                    f"تم تسجيل الإغلاق — الفرق {self.money(result['variance'])}"
                    + (" (رُحّلت تسوية)" if result.get("adjustment_posted") else ""),
                    kind="success",
                    sound_kind="save",
                )
                if print_after.value and self.native_files is not None:
                    try:
                        movement = self.ctx.cash_day_close.today_cash_movement()
                        try:
                            today_summary = self.ctx.dashboard.today_summary()
                            today_summary = {
                                "today_sales": float(
                                    today_summary.get("total") or today_summary.get("sales") or 0
                                )
                            }
                        except Exception:
                            today_summary = {}
                        html = self.ctx.documents.day_close_html(
                            result, movement=movement, summary=today_summary
                        )

                        async def print_selected(layout: str, width: int):
                            try:
                                await self.native_files.print_html(
                                    apply_print_profile(html, layout, width),
                                    name="nano-day-close",
                                    paper_layout=layout,
                                    roll_width_mm=width,
                                )
                            except Exception as exc:
                                self.notify(str(exc), kind="error")

                        open_print_profile_dialog(
                            self.page,
                            self.ctx.settings,
                            print_selected,
                            surface="dashboard",
                            native_files=self.native_files,
                            preview_builder=lambda _layout, _width: html,
                        )
                    except Exception as exc:
                        self.notify(f"تعذر تجهيز تقرير الإغلاق: {exc}", kind="warning")
                self.show_overview()
                self._changed()

            counted_field.on_change = update_preview
            update_preview()
            dialog.content = ft.Column(
                [counted_field, note_field, preview, post_adjustment, print_after],
                tight=True,
                spacing=10,
                width=520,
            )
            dialog.actions = [
                ft.TextButton("إلغاء", on_click=lambda _: self.page.close(dialog)),
                primary_button("تسجيل الإغلاق", icon=ft.Icons.CHECK, on_click=save_close),
            ]
            self.page.open(dialog)

        header = ft.Container(
            ft.Column(
                [
                    self._section_nav("overview"),
                    ft.Row(
                        [ft.Text(f"ملخص حتى {today}", color=Colors.TEXT_SECONDARY),
                         ft.Container(expand=True),
                         ft.OutlinedButton("تصدير المؤشرات", icon=ft.Icons.DOWNLOAD_OUTLINED, on_click=export_finance_snapshot),
                         primary_button("مطابقة وإغلاق الصندوق", icon=ft.Icons.CHECK_CIRCLE_OUTLINE, on_click=close_cash_day)],
                        spacing=10,
                    ),
                ],
                spacing=10,
            ),
            padding=ft.padding.only(left=16, right=16, top=14, bottom=12),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        body = ft.Column(
            [
                summary,
                ft.Text("أولوية التحصيل", size=15, weight=ft.FontWeight.BOLD),
                ft.Text("الترتيب حسب الفواتير المتأخرة الفعلية وتاريخ الاستحقاق المحفوظ؛ غير المحدد لا يُعد متأخرًا.", size=11, color=Colors.TEXT_SECONDARY),
                collection_grid,
                ft.Text("الالتزامات والاستحقاقات", size=15, weight=ft.FontWeight.BOLD),
                ft.Text(dated_note, size=11, color=Colors.TEXT_SECONDARY),
                commitment_grid,
                ft.Text("مصروفات أعلى من المعتاد", size=15, weight=ft.FontWeight.BOLD),
                ft.Text(
                    f"مقارنة الشهر حتى اليوم بمتوسط {anomalies['baseline_months']} أشهر سابقة مع مراعاة الأيام المنقضية. "
                    "إشارة للمراجعة وليست حكمًا بوجود خطأ.",
                    size=11, color=Colors.TEXT_SECONDARY,
                ),
                anomaly_grid,
                ft.Text(close_note, size=11, color=Colors.TEXT_SECONDARY),
                ft.Container(height=20),
            ],
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self.content.content = ft.Column(
            [header, ft.Container(body, expand=True, padding=16)],
            spacing=0,
            expand=True,
        )
        self.page.update()

    def show_vouchers(self) -> None:
        self._clear_context_actions()
        self._active_finance_view = "vouchers"
        self._set_header("السندات", "سندات القبض والدفع والمصروفات وربطها بالفواتير")
        search = SelectAllTextField(
            label="بحث في السندات",
            hint_text="رقم السند، الحساب أو المرجع",
            prefix_icon=ft.Icons.SEARCH,
            on_focus=lambda e: grid.disarm(),
        )
        date_from = SmartDateField(label="من تاريخ", hint_text="YYYY-MM-DD")
        date_to = SmartDateField(label="إلى تاريخ", hint_text="YYYY-MM-DD")
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
            auto_hidden=set(),
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
                        danger_button("حذف المحدد", icon=ft.Icons.DELETE_OUTLINE, on_click=confirm_bulk_delete, size="md"),
                        ft.TextButton("إلغاء التحديد", on_click=lambda _: grid.clear_selection()),
                    ],
                    spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
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
                danger_button("حذف", on_click=yes),
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
            q = (search.value or "").strip().casefold()
            start = (date_from.value or "").strip()
            end = (date_to.value or "").strip()
            filtered = []
            for v in vouchers:
                if filter_state["type"] != "all" and v.get("voucher_type") != filter_state["type"]:
                    continue
                voucher_date = str(v.get("voucher_date") or "")
                if start and voucher_date < start:
                    continue
                if end and voucher_date > end:
                    continue
                hay = f"{v.get('id','')} {v.get('party_name','')} {v.get('reference','')}".casefold()
                if q and q not in hay:
                    continue
                filtered.append(v)
            receipts = sum(float(v.get("amount") or 0) for v in filtered if v.get("voucher_type") == "receipt")
            payments = sum(float(v.get("amount") or 0) for v in filtered if v.get("voucher_type") == "payment")
            unallocated_total = sum(float(v.get("unallocated_amount") or 0) for v in filtered)
            summary.controls = metric_controls([
                kpi_card("إجمالي القبض", "", ft.Icons.ADD_CARD, Colors.SUCCESS, on_tap=lambda _: set_filter("receipt"), amount_usd=receipts, settings=self.ctx.settings),
                kpi_card("إجمالي الدفع", "", ft.Icons.PAYMENTS_OUTLINED, Colors.DANGER, on_tap=lambda _: set_filter("payment"), amount_usd=payments, settings=self.ctx.settings),
                kpi_card("غير موزع في النتائج", "", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.PRIMARY, amount_usd=unallocated_total, settings=self.ctx.settings),
            ])
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

        def set_period(period: str):
            today_value = date.today()
            if period == "month":
                date_from.value, date_to.value = today_value.replace(day=1).isoformat(), today_value.isoformat()
            elif period == "30":
                date_from.value, date_to.value = (today_value - timedelta(days=29)).isoformat(), today_value.isoformat()
            else:
                date_from.value, date_to.value = "", ""
            render_limit["n"] = 60
            refresh()

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
                        [
                            ft.Container(search, width=280),
                            type_filter,
                            ft.Container(expand=True),
                            primary_button(
                                "سند قبض",
                                icon=ft.Icons.ADD_CARD,
                                on_click=lambda _: self.show_voucher_dialog(None, "receipt"),
                                width=None,
                                size="md",
                            ),
                            secondary_button(
                                "سند دفع",
                                icon=ft.Icons.PAYMENTS_OUTLINED,
                                on_click=lambda _: self.show_voucher_dialog(None, "payment"),
                                width=None,
                                size="md",
                            ),
                            secondary_button(
                                "مصروف جديد",
                                icon=ft.Icons.RECEIPT_OUTLINED,
                                on_click=lambda _: self.show_expense_dialog(),
                                width=None,
                                size="md",
                            ),
                            columns_button,
                            export_button,
                            density_button,
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            date_from, date_to,
                            ft.OutlinedButton("هذا الشهر", on_click=lambda _: set_period("month")),
                            ft.OutlinedButton("آخر 30 يومًا", on_click=lambda _: set_period("30")),
                            ft.OutlinedButton("كل الفترة", on_click=lambda _: set_period("all")),
                            ft.FilledButton("تطبيق", icon=ft.Icons.FILTER_ALT_OUTLINED, on_click=refresh_from_search),
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row([summary_toggle], alignment=ft.MainAxisAlignment.END),
                ],
                spacing=10,
            ),
            padding=ft.padding.only(left=16, right=16, top=12, bottom=10),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        scroll_body = ft.Container(
            ft.Column(
                [
                    ft.Text("ملخص القيم مطابق لنتائج البحث والتصفية والفترة المحددة.", size=10, color=Colors.TEXT_SECONDARY),
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
        refresh()

    def _voucher_html(self, voucher: dict) -> str:
        # Delegates to the shared DocumentService shell so the voucher shares
        # the exact branding header, typography, money formatting and print
        # profile of every other document (invoice/statement/day-close).
        return self.ctx.documents.voucher_html(voucher)

    def _open_payment_source(self, payment_id: int) -> None:
        """Drill-down for a ledger 'payment' movement: a real voucher opens
        its detail sheet, while an invoice's own initial payment (a derived
        reflection row, not a real voucher -- and its id lives in a
        different id space than vouchers) opens the invoice itself, so a
        tap can never land on an unrelated voucher that merely shares the
        numeric id."""
        try:
            prow = self.ctx.payments.payment_source(payment_id)
        except Exception:
            prow = None
        if prow is not None and str(prow.get("source_type")) == "invoice_initial" and prow.get("source_id") is not None:
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
        detail_sheet, sheet_w = sheets.new_desktop_sheet(self.page)

        def close(_=None): self.page.close(detail_sheet)
        def edit(_=None): close(); self.show_voucher_dialog(voucher_id)
        def duplicate(_=None):
            close(); self.show_voucher_dialog(None, voucher.get("voucher_type") or "receipt", initial_data=voucher)
        async def print_voucher(_=None):
            if self.native_files is None:
                self.notify("الطباعة الأصلية غير مهيأة في هذا البناء"); return

            def voucher_document_html() -> str:
                return self._voucher_html(voucher)

            async def print_selected(layout: str, width: int) -> None:
                try:
                    html = apply_print_profile(voucher_document_html(), layout, width)
                    await self.native_files.print_html(
                        html,
                        name=f"nano-voucher-{voucher_id}",
                        paper_layout=layout,
                        roll_width_mm=width,
                    )
                except Exception as exc:
                    self.notify(str(exc), kind="error")

            open_print_profile_dialog(
                self.page,
                self.ctx.settings,
                print_selected,
                surface="voucher",
                native_files=self.native_files,
                preview_builder=lambda _layout, _width: voucher_document_html(),
            )

        # Wide desktop detail sheet: fixed comfortable height, scrollable
        # body, pinned action footer.
        sheet_h = sheets.sheet_height(self.page, sheets.DETAIL_HEIGHT_RATIO, min_height=440, max_height=680)

        detail_sheet.content = sheets.sheet_frame(
            ft.Column(
                [
                    sheets.sheet_handle(),
                    ft.Text(f"{title} #{voucher_id}", size=18, weight=ft.FontWeight.BOLD),
                    ft.Container(
                        ft.Column([
                            ft.Container(ft.Column([money_row(voucher.get("amount"), self.ctx.settings, size=28, weight=ft.FontWeight.BOLD, color=accent), ft.Text(title, size=11, color=Colors.TEXT_SECONDARY)], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2), padding=14, bgcolor=Colors.BACKGROUND, border_radius=16),
                            ft.ResponsiveRow([
                                ft.Container(ft.Text(f"التاريخ: {voucher.get('voucher_date') or '—'}", size=11), col={"xs": 6, "md": 3}),
                                ft.Container(ft.Text(f"الحساب: {voucher.get('party_name') or '—'}", size=11), col={"xs": 6, "md": 3}),
                                ft.Container(ft.Text(f"المرجع: {voucher.get('reference') or '—'}", size=11), col={"xs": 6, "md": 3}),
                                ft.Container(ft.Row([ft.Text("على الحساب: ", size=11), money_row(voucher.get("unallocated_amount"), self.ctx.settings, size=11)], spacing=2, tight=True), col={"xs": 6, "md": 3}),
                            ]),
                            ft.Text(voucher.get("notes") or "بدون ملاحظات", size=10, color=Colors.TEXT_SECONDARY),
                            ft.Divider(height=8), ft.Text("التوزيعات", size=13, weight=ft.FontWeight.BOLD), ft.Column(alloc_controls, spacing=5),
                        ], spacing=8, scroll=ft.ScrollMode.AUTO),
                        expand=True, padding=ft.padding.only(top=4),
                    ),
                    ft.Container(
                        ft.Row(
                            [
                                ft.TextButton("حذف", icon=ft.Icons.DELETE_OUTLINE, on_click=lambda _: (close(), self.confirm_delete_voucher(voucher_id))),
                                ft.TextButton("تكرار", icon=ft.Icons.CONTENT_COPY, on_click=duplicate),
                                secondary_button("طباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=print_voucher, width=None, size="md"),
                                secondary_button("تعديل", icon=ft.Icons.EDIT_OUTLINED, on_click=edit, width=None, size="md"),
                                primary_button("إغلاق", on_click=close, width=None, size="md"),
                            ],
                            spacing=8, run_spacing=8, wrap=True,
                        ),
                        padding=ft.padding.only(top=4),
                    ),
                ],
                spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            width=sheet_w,
            height=sheet_h,
        )
        self.page.open(detail_sheet)

    def show_voucher_dialog(
        self,
        voucher_id: int | None = None,
        voucher_type: str = "receipt",
        initial_data: dict | None = None,
        *,
        refresh_vouchers: bool = True,
    ) -> None:
        existing = self.ctx.payments.get_voucher(voucher_id) if voucher_id else None
        seed = existing or initial_data or {}
        if voucher_id and not existing:
            self.notify("السند غير موجود")
            return

        current_type = existing["voucher_type"] if existing else seed.get("voucher_type", voucher_type)
        type_dd = ft.Dropdown(
            label="نوع السند",
            value=current_type,
            options=[
                ft.dropdown.Option(key="receipt", text="قبض من عميل"),
                ft.dropdown.Option(key="payment", text="دفع لمورد"),
            ],
            filled=True,
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.MD,
            border_color=Colors.BORDER,
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
        allocation_mode = ft.Dropdown(
            label="توزيع الدفعة",
            value=default_mode,
            options=[
                ft.dropdown.Option(key="oldest", text="الأقدم أولًا تلقائيًا"),
                ft.dropdown.Option(key="manual", text="توزيع يدوي"),
                ft.dropdown.Option(key="none", text="رصيد على الحساب دون توزيع"),
            ],
            filled=True,
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.MD,
            border_color=Colors.BORDER,
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
                if refresh_vouchers:
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
        dialog.actions = [ft.TextButton("إلغاء", on_click=close), danger_button("حذف", icon=ft.Icons.DELETE_FOREVER, on_click=delete)]
        self.page.open(dialog)

    def show_expenses(self) -> None:
        self._clear_context_actions()
        self._active_finance_view = "expenses"
        self._set_header("المصروفات", "تسجيل ومتابعة مصروفات النشاط")
        search = SelectAllTextField(
            label="بحث في المصروفات",
            hint_text="البيان، التصنيف أو المرجع",
            prefix_icon=ft.Icons.SEARCH,
            on_focus=lambda e: grid.disarm(),
        )
        date_from = SmartDateField(label="من تاريخ", hint_text="YYYY-MM-DD")
        date_to = SmartDateField(label="إلى تاريخ", hint_text="YYYY-MM-DD")
        summary = ft.ResponsiveRow(spacing=8, run_spacing=8)
        summary_toggle = self._summary_toggle(summary)
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
            auto_hidden=set(),
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
                        danger_button("حذف المحدد", icon=ft.Icons.DELETE_OUTLINE, on_click=confirm_bulk_delete, size="md"),
                        ft.TextButton("إلغاء التحديد", on_click=lambda _: grid.clear_selection()),
                    ],
                    spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
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
                danger_button("حذف", on_click=yes),
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

        def set_period(period: str):
            today_value = date.today()
            if period == "month":
                date_from.value, date_to.value = today_value.replace(day=1).isoformat(), today_value.isoformat()
            elif period == "30":
                date_from.value, date_to.value = (today_value - timedelta(days=29)).isoformat(), today_value.isoformat()
            else:
                date_from.value, date_to.value = "", ""
            render_limit["n"] = 60
            refresh()

        def refresh(_=None):
            expenses = self.ctx.expenses.list_expenses()
            q = (search.value or "").strip().casefold()
            start = (date_from.value or "").strip()
            end = (date_to.value or "").strip()
            filtered = [
                e for e in expenses
                if (not q or q in f"{e.get('description','')} {e.get('category_name','')} {e.get('reference','')}".casefold())
                and (not start or str(e.get("expense_date") or "") >= start)
                and (not end or str(e.get("expense_date") or "") <= end)
            ]
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
            total = sum(float(e.get("amount") or 0) for e in filtered)
            categories = {str(e.get("category_name") or "بلا تصنيف") for e in filtered}
            summary.controls = metric_controls([
                kpi_card("إجمالي المصروفات في النتائج", "", ft.Icons.PAYMENTS_OUTLINED, Colors.DANGER_DARK, amount_usd=total, settings=self.ctx.settings),
                kpi_card("عدد المصروفات", str(len(filtered)), ft.Icons.RECEIPT_OUTLINED, Colors.PRIMARY),
                kpi_card("التصنيفات", str(len(categories)), ft.Icons.CATEGORY_OUTLINED, Colors.PURPLE),
            ])
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
                        [
                            ft.Container(search, width=280),
                            primary_button(
                                "مصروف جديد",
                                icon=ft.Icons.ADD,
                                on_click=lambda _: self.show_expense_dialog(),
                                width=None,
                                size="md",
                            ),
                            ft.Container(expand=True),
                            columns_button,
                            export_button,
                            density_button,
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            date_from, date_to,
                            ft.OutlinedButton("هذا الشهر", on_click=lambda _: set_period("month")),
                            ft.OutlinedButton("آخر 30 يومًا", on_click=lambda _: set_period("30")),
                            ft.OutlinedButton("كل الفترة", on_click=lambda _: set_period("all")),
                            ft.FilledButton("تطبيق", icon=ft.Icons.FILTER_ALT_OUTLINED, on_click=refresh_from_search),
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
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
        description_value = str(existing.get("description") or "مصروف") if existing else "مصروف"
        reference_value = str(existing.get("reference") or "") if existing else ""
        amount = SmartAmountField(
            label=currency.amount_field_label("المبلغ", self.ctx.settings),
            value=currency.to_input_text(existing.get("amount", 0) if existing else 0, self.ctx.settings),
        )
        edate = SmartDateField(label="التاريخ", value=str(existing["expense_date"] if existing else date.today().isoformat()))
        notes = SelectAllTextField(label="ملاحظات", value=(existing.get("notes") or "") if existing else "", multiline=True)
        page_w = int(self.page.width or 1280)
        sheet_w = max(320, min(1080, page_w - 32))
        sheet = ft.BottomSheet(
            content=ft.Container(),
            is_scroll_controlled=True,
            enable_drag=True,
            maintain_bottom_view_insets_padding=True,
            size_constraints=ft.BoxConstraints(min_width=sheet_w, max_width=sheet_w),
        )

        def close(_=None):
            self.page.close(sheet)

        def save(_):
            try:
                kwargs = {
                    "amount": currency.parse_display_input(amount.value, self.ctx.settings),
                    "description": description_value,
                    "expense_date": edate.value,
                    "category_id": int(existing["category_id"]) if existing and existing.get("category_id") else None,
                    "reference": reference_value,
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
            fields=[
                ft.ResponsiveRow(
                    [
                        ft.Container(amount, col={"xs": 12, "md": 6}),
                        ft.Container(edate, col={"xs": 12, "md": 6}),
                        ft.Container(notes, col={"xs": 12, "md": 6}),
                        ft.Container(
                            ft.Column(
                                [
                                    ft.Text("إيصال (اختياري)", size=12, weight=ft.FontWeight.W_600),
                                    photo_area,
                                    ft.Row(photo_buttons, wrap=True),
                                ],
                                spacing=8,
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=12,
                            bgcolor=Colors.BACKGROUND,
                            border=ft.border.all(1, Colors.BORDER),
                            border_radius=Radius.MD,
                            col={"xs": 12, "md": 6},
                        ),
                    ],
                    spacing=12,
                    run_spacing=10,
                    rtl=True,
                )
            ],
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
        dialog.actions = [ft.TextButton("إلغاء", on_click=close), danger_button("حذف", on_click=delete)]
        self.page.open(dialog)

    def show_statements(self, party_type: str, party_id: int | None = None) -> None:
        self._clear_context_actions()
        self._active_finance_view = "customers" if party_type == "customer" else "suppliers"
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
        statement_rows: list[dict] = []
        export_rows: list[dict] = []
        sort_state = {"key": None, "direction": None}

        def open_statement_source(row: dict) -> None:
            source_type = str(row.get("source_type") or "")
            try:
                source_id = int(row["source_id"]) if row.get("source_id") is not None else None
            except (TypeError, ValueError):
                source_id = None
            if source_id is None:
                return
            if source_type == "invoice" and self.on_open_invoice:
                self.on_open_invoice(source_id)
            elif source_type == "payment":
                self._open_payment_source(source_id)

        def sort_statement(key: str | None, direction: str | None) -> None:
            sort_state.update(key=key, direction=direction)
            export_rows[:] = list(statement_rows)
            if key:
                reverse = direction == "desc"
                export_rows.sort(
                    key=lambda row: (
                        float(row.get(key) or 0)
                        if key in {"debit", "credit", "movement", "balance"}
                        else str(row.get(key) or "")
                    ),
                    reverse=reverse,
                )
            statement_grid.set_rows(export_rows)

        statement_grid = DataGrid(
            columns=PARTY_STATEMENT_GRID_COLUMNS,
            rows=[],
            page=self.page,
            viewport_height=max(320, min(600, int((self.page.height or 760) * 0.55))),
            boxed=True,
            center_cells=True,
            on_sort=sort_statement,
            on_row_activate=open_statement_source,
            on_row_menu=lambda row: [
                ("فتح المستند المصدر", ft.Icons.OPEN_IN_NEW_ROUNDED,
                 lambda selected=row: open_statement_source(selected))
            ] if row.get("source_id") is not None and row.get("source_type") in {"invoice", "payment"} else [],
            activate_tooltip="فتح المستند المصدر",
            empty_text="لا توجد حركات في الفترة المحددة",
            formatter=lambda col, value, row: (
                self.money(value) if col.kind == "money"
                else str(value or row.get("source_label") or "—")
            ),
        )

        async def export_statement_csv(_=None):
            if not export_rows:
                self.notify("لا توجد حركات للتصدير", kind="warning")
                return
            if self.native_files is None:
                self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                path = Path(tempfile.gettempdir()) / f"nano_statement_{party_type}_{party_dd.value}_{datetime.now():%Y%m%d_%H%M%S}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = _csv.writer(stream)
                    writer.writerow(["التاريخ", "المصدر", "البيان", "مدين", "دائن", "الحركة", "الرصيد"])
                    for row in export_rows:
                        writer.writerow([
                            row.get("entry_date"), row.get("source_label"), row.get("description"),
                            row.get("debit"), row.get("credit"), row.get("movement"), row.get("balance"),
                        ])
                await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير كشف الحساب")
            except Exception as exc:
                self.notify(str(exc), kind="error")

        def statement_document_html() -> str:
            return self.ctx.documents.statement_html(
                party_type,
                int(party_dd.value),
                date_from=(date_from.value or "").strip() or None,
                date_to=(date_to.value or "").strip() or None,
            )

        async def print_statement(_):
            if not party_dd.value:
                self.notify("اختر الحساب أولًا")
                return
            if self.native_files is None:
                self.notify("الطباعة الأصلية غير مهيأة في هذا البناء")
                return
            async def print_selected(layout: str, width: int) -> None:
                try:
                    html = apply_print_profile(statement_document_html(), layout, width)
                    await self.native_files.print_html(
                        html,
                        name=f"nano-statement-{party_type}-{party_dd.value}",
                        paper_layout=layout,
                        roll_width_mm=width,
                    )
                except Exception as exc:
                    self.notify(str(exc), kind="error")

            open_print_profile_dialog(
                self.page,
                self.ctx.settings,
                print_selected,
                surface="statement",
                native_files=self.native_files,
                preview_builder=lambda _layout, _width: statement_document_html(),
            )

        async def share_statement_pdf(_):
            if not party_dd.value:
                self.notify("اختر الحساب أولًا")
                return
            if self.native_files is None:
                self.notify("تصدير PDF غير مهيأ في هذا البناء")
                return
            async def share_selected(layout: str, width: int) -> None:
                try:
                    html = apply_print_profile(statement_document_html(), layout, width)
                    await self.native_files.share_pdf(
                        html,
                        filename=f"nano_statement_{party_type}_{party_dd.value}.pdf",
                        paper_layout=layout,
                        roll_width_mm=width,
                    )
                except Exception as exc:
                    self.notify(str(exc), kind="error")

            open_print_profile_dialog(
                self.page,
                self.ctx.settings,
                share_selected,
                title="تنسيق كشف PDF",
                surface="statement",
                native_files=self.native_files,
                preview_builder=lambda _layout, _width: statement_document_html(),
            )

        export_actions.controls = [
            ft.OutlinedButton("طباعة الكشف", icon=ft.Icons.PRINT_OUTLINED, on_click=print_statement),
            ft.OutlinedButton("مشاركة PDF", icon=ft.Icons.PICTURE_AS_PDF_OUTLINED, on_click=share_statement_pdf),
            ft.OutlinedButton("تصدير CSV", icon=ft.Icons.DOWNLOAD_OUTLINED, on_click=export_statement_csv),
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
                result.controls.append(
                    metric_row([
                        kpi_card("رصيد نهاية الفترة", "", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.PRIMARY, amount_usd=data["closing_balance"], settings=self.ctx.settings),
                        kpi_card("رصيد بداية الفترة", "", ft.Icons.HISTORY_OUTLINED, Colors.PURPLE, amount_usd=data["opening_balance"], settings=self.ctx.settings),
                        kpi_card("إجمالي المدين", "", ft.Icons.ARROW_DOWNWARD, Colors.PRIMARY, amount_usd=data["debit_total"], settings=self.ctx.settings),
                        kpi_card("إجمالي الدائن", "", ft.Icons.ARROW_UPWARD, Colors.PURPLE, amount_usd=data["credit_total"], settings=self.ctx.settings),
                        kpi_card("فواتير مفتوحة", str(len(data["open_invoices"])), ft.Icons.RECEIPT_LONG_OUTLINED, Colors.ORANGE),
                    ])
                )
                statement_rows[:] = list(data["rows"])
                export_rows[:] = list(statement_rows)
                if sort_state["key"]:
                    sort_statement(sort_state["key"], sort_state["direction"])
                else:
                    statement_grid.set_rows(export_rows)
                result.controls.append(statement_grid)
            except Exception as exc:
                export_actions.visible = False
                self.notify(str(exc), kind="error")
            self.page.update()

        def render_from_filters(_=None):
            render()

        party_dd.on_change = render_from_filters
        def set_period(period: str):
            today_value = date.today()
            if period == "month":
                date_from.value = today_value.replace(day=1).isoformat()
                date_to.value = today_value.isoformat()
            elif period == "30":
                date_from.value = (today_value - timedelta(days=29)).isoformat()
                date_to.value = today_value.isoformat()
            else:
                date_from.value = ""
                date_to.value = ""
            render_from_filters()
        # Same sticky-header / scrolling-body split as the other finance
        # sections: section tabs and the account/date filters stay pinned,
        # the KPI cards and statement rows scroll beneath them.
        sticky_header = ft.Container(
            ft.Column(
                [
                    self._section_nav(active),
                    ft.Row(
                        [
                            ft.Container(party_dd, width=260),
                            ft.Container(date_from, width=180),
                            ft.Container(date_to, width=180),
                            ft.OutlinedButton("هذا الشهر", on_click=lambda _: set_period("month")),
                            ft.OutlinedButton("آخر 30 يومًا", on_click=lambda _: set_period("30")),
                            ft.OutlinedButton("كل الفترة", on_click=lambda _: set_period("all")),
                            ft.FilledButton("عرض الكشف", icon=ft.Icons.SEARCH, on_click=render_from_filters),
                            export_actions,
                        ],
                        wrap=True,
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
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
        """General-ledger hub with sortable trial-balance and activity tables."""
        self._clear_context_actions()
        self._active_finance_view = "ledger"
        self._set_header("دفتر الأستاذ", "ميزان المراجعة وكشوف الحسابات من القيود المرحلة")
        account_dd = SearchSelect(
            label="الحساب",
            choices=[(code, label) for code, (label, _) in StatementService.ACCOUNT_LABELS.items()],
        )
        date_from = SmartDateField(label="من تاريخ", hint_text="YYYY-MM-DD")
        date_to = SmartDateField(label="إلى تاريخ", hint_text="YYYY-MM-DD")
        result = ft.Column(spacing=8)
        trial_rows: list[dict] = []
        account_rows: list[dict] = []
        export_rows: list[dict] = []
        trial_sort = {"key": None, "direction": None}
        ledger_sort = {"key": None, "direction": None}

        def sort_rows(rows: list[dict], key: str | None, direction: str | None, numeric: set[str]) -> list[dict]:
            ordered = list(rows)
            if key:
                ordered.sort(
                    key=lambda row: float(row.get(key) or 0) if key in numeric else str(row.get(key) or ""),
                    reverse=direction == "desc",
                )
            return ordered

        def sort_trial(key: str | None, direction: str | None) -> None:
            trial_sort.update(key=key, direction=direction)
            trial_grid.set_rows(sort_rows(trial_rows, key, direction, {"debit", "credit", "balance"}))

        def sort_ledger(key: str | None, direction: str | None) -> None:
            ledger_sort.update(key=key, direction=direction)
            export_rows[:] = sort_rows(account_rows, key, direction, {"debit", "credit", "movement", "balance"})
            ledger_grid.set_rows(export_rows)

        def pick_account(row: dict) -> None:
            account_dd.value = str(row.get("code") or "")
            try:
                account_dd.update()
            except Exception:
                pass
            render_from_filters()

        def open_ledger_source(row: dict) -> None:
            source_type = str(row.get("source_type") or "")
            try:
                source_id = int(row["source_id"]) if row.get("source_id") is not None else None
            except (TypeError, ValueError):
                source_id = None
            if source_id is None:
                return
            if source_type == "invoice" and self.on_open_invoice:
                self.on_open_invoice(source_id)
            elif source_type == "payment":
                self._open_payment_source(source_id)

        trial_grid = DataGrid(
            columns=TRIAL_BALANCE_GRID_COLUMNS,
            rows=[],
            viewport_height=250,
            boxed=True,
            center_cells=True,
            on_sort=sort_trial,
            on_row_activate=pick_account,
            on_row_menu=lambda row: [
                ("عرض حركات الحساب", ft.Icons.OPEN_IN_NEW_ROUNDED,
                 lambda selected=row: pick_account(selected))
            ],
            activate_tooltip="عرض حركات الحساب",
            empty_text="لا توجد قيود",
            formatter=lambda col, value, row: self.money(value) if col.kind == "money" else str(value or ""),
        )
        ledger_grid = DataGrid(
            columns=LEDGER_GRID_COLUMNS,
            rows=[],
            viewport_height=max(320, min(600, int((self.page.height or 760) * 0.55))),
            boxed=True,
            center_cells=True,
            on_sort=sort_ledger,
            on_row_activate=open_ledger_source,
            on_row_menu=lambda row: [
                ("فتح المستند المصدر", ft.Icons.OPEN_IN_NEW_ROUNDED,
                 lambda selected=row: open_ledger_source(selected))
            ] if row.get("source_id") is not None and row.get("source_type") in {"invoice", "payment"} else [],
            activate_tooltip="فتح المستند المصدر",
            empty_text="لا توجد حركات لهذا الحساب في الفترة المحددة",
            formatter=lambda col, value, row: (
                self.money(value) if col.kind == "money"
                else str(value or row.get("party_name") or "—")
            ),
        )
        export_button = ft.OutlinedButton("تصدير CSV", icon=ft.Icons.DOWNLOAD_OUTLINED)

        async def export_ledger(_=None):
            if self.native_files is None:
                self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                if account_dd.value:
                    rows = export_rows
                    filename = "nano_general_ledger"
                    headers = ["التاريخ", "المصدر", "الطرف", "البيان", "مدين", "دائن", "الحركة", "الرصيد"]
                    values = lambda row: [
                        row.get("entry_date"), row.get("source_label"), row.get("party_name"),
                        row.get("description"), row.get("debit"), row.get("credit"),
                        row.get("movement"), row.get("balance"),
                    ]
                else:
                    rows = sort_rows(trial_rows, trial_sort["key"], trial_sort["direction"], {"debit", "credit", "balance"})
                    filename = "nano_trial_balance"
                    headers = ["الحساب", "اسم الحساب", "مدين", "دائن", "الرصيد"]
                    values = lambda row: [row.get("code"), row.get("label"), row.get("debit"), row.get("credit"), row.get("balance")]
                if not rows:
                    self.notify("لا توجد بيانات للتصدير", kind="warning")
                    return
                path = Path(tempfile.gettempdir()) / f"{filename}_{datetime.now():%Y%m%d_%H%M%S}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = _csv.writer(stream)
                    writer.writerow(headers)
                    writer.writerows(values(row) for row in rows)
                await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير دفتر الأستاذ")
            except Exception as exc:
                self.notify(str(exc), kind="error")

        export_button.on_click = export_ledger

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
            trial_rows[:] = list(tb["accounts"])
            trial_grid.set_rows(sort_rows(
                trial_rows, trial_sort["key"], trial_sort["direction"], {"debit", "credit", "balance"}
            ))
            result.controls.append(trial_grid)

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
            account_rows[:] = list(data["rows"])
            export_rows[:] = sort_rows(
                account_rows, ledger_sort["key"], ledger_sort["direction"],
                {"debit", "credit", "movement", "balance"},
            )
            ledger_grid.set_rows(export_rows)
            result.controls.append(ledger_grid)

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
            render()

        account_dd.on_change = render_from_filters
        def set_period(period: str):
            today_value = date.today()
            if period == "month":
                date_from.value = today_value.replace(day=1).isoformat()
                date_to.value = today_value.isoformat()
            elif period == "30":
                date_from.value = (today_value - timedelta(days=29)).isoformat()
                date_to.value = today_value.isoformat()
            else:
                date_from.value = ""
                date_to.value = ""
            render_from_filters()

        sticky_header = ft.Container(
            ft.Column(
                [
                    self._section_nav("ledger"),
                    ft.Row(
                        [
                            ft.Container(account_dd, width=260),
                            ft.Container(date_from, width=180),
                            ft.Container(date_to, width=180),
                            ft.OutlinedButton("هذا الشهر", on_click=lambda _: set_period("month")),
                            ft.OutlinedButton("آخر 30 يومًا", on_click=lambda _: set_period("30")),
                            ft.OutlinedButton("كل الفترة", on_click=lambda _: set_period("all")),
                            export_button,
                            ft.FilledButton("عرض", icon=ft.Icons.SEARCH, on_click=render_from_filters),
                        ],
                        wrap=True,
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
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
