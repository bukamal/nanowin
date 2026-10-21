from __future__ import annotations

import asyncio
import csv as _csv
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import flet as ft

from nano_offline.core.toast import toast
from nano_offline.core.home_widget import refresh_home_widget

from nano_offline.components import (
    SearchSelect,
    SegmentedToggle,
    SegmentOption,
    SelectAllTextField,
    SmartAmountField,
    SmartDateField,
    empty_state,
    kpi_card,
    new_form_sheet,
    render_form_sheet,
    status_pill,
    DataGrid,
    GridColumn,
    metric_row,
)

from nano_offline.services.invoice_service import InvoiceLineInput
from nano_offline.components.buttons import stepper_icon_button, header_close_button
from nano_offline.components.money_text import money_row
from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.core import currency
from nano_offline.core.margin_guard import check_sale_margin
from nano_offline.core.price_fingerprint import check_purchase_price
from nano_offline.core import invoice_settings
from nano_offline.core import barcode_settings
from nano_offline.core.platform_capabilities import supports

# Below this remaining quantity a stock item is flagged on the invoice line
# as running low -- there is no per-item reorder-point column in the schema
# yet, so this is a fixed, conservative default rather than a per-item
# setting.
LOW_STOCK_THRESHOLD = 5.0


def _default_invoice_unit(units: list[dict]) -> dict | None:
    """Prefer piece for new invoice lines, then the item's base unit."""
    if not units:
        return None
    piece = next((unit for unit in units if str(unit.get("name") or "").strip() == "قطعة"), None)
    if piece is not None:
        return piece
    return next((unit for unit in units if unit.get("is_base")), units[0])


STATUS_AR = {
    "paid": "مدفوعة",
    "partial": "مدفوعة جزئيًا",
    "unpaid": "غير مدفوعة",
}

# Invoice list grid columns -- same table language as the materials screen.
# Every cell is display-only on purpose: an invoice is a financial record, so
# nothing is edited in place; tapping a row (or its open icon) opens the
# detail sheet, which carries payment/print/PDF/edit/delete.
INVOICE_GRID_COLUMNS = [
    GridColumn("id", "الفاتورة", width=80, align="center", kind="number", tooltip="رقم الفاتورة — اضغط للترتيب"),
    GridColumn("type", "النوع", width=76, align="center", kind="badge", tooltip="بيع (مبيعات) أو شراء (مشتريات)"),
    GridColumn("party_name", "الطرف", tooltip="العميل في المبيعات أو المورد في المشتريات — نقدي عند عدم التحديد"),
    GridColumn("invoice_date", "التاريخ", width=98, align="center", tooltip="تاريخ الفاتورة — الافتراضي ترتيب زمني مع تجميع (اليوم/أمس…)"),
    GridColumn("total", "الإجمالي", width=106, align="end", kind="money", tooltip="إجمالي الفاتورة بالمبلغ المعروض"),
    GridColumn("remaining_amount", "المتبقي", width=106, align="end", kind="money", tooltip="ما لم يُسدَّد بعد — أحمر عند التأخر عن السداد"),
    GridColumn("payment_status", "الحالة", width=142, align="center", kind="badge", sortable=False, tooltip="حالة السداد، وتظهر مؤشرات التأخير بالأيام"),
]


class InvoiceCenter:
    """Responsive invoice center/editor for Flet 0.28.x."""

    def __init__(self, page: ft.Page, ctx, content: ft.Container, on_saved=None, native_files=None, on_title_change=None):
        self.page = page
        self.ctx = ctx
        self.content = content
        self.on_saved = on_saved
        self.native_files = native_files
        self.on_title_change = on_title_change
        self.invoice_type = "sale"
        self.editing_id: int | None = None
        self.lines: list[dict] = []

    def money(self, value: float) -> str:
        return currency.format_amount(value, self.ctx.settings)

    @staticmethod
    def _qty(value) -> str:
        """Plain (non-currency) number formatting -- quantities, unit ratios.

        No thousands separators and no forced decimals: whole amounts render
        as "15", fractional ones keep only the decimals that matter ("12.5"),
        so quantities never show comma separators in invoice lines.
        """
        v = float(value or 0)
        if v == int(v):
            return str(int(v))
        return f"{v:.3f}".rstrip("0").rstrip(".")

    def notify(self, text: str, kind: str | None = None, sound_kind: str | None = None) -> None:
        toast(self.page, text, kind=kind, sound_kind=sound_kind)


    def _log_invoice_print(self, invoice_id: int, *, channel: str = "print") -> None:
        """Record reprint activity in audit_log for management transparency."""
        try:
            session = self.ctx.auth.current()
            username = getattr(session, "username", None) if session else None
            user_id = getattr(session, "id", None) if session else None
        except Exception:
            username, user_id = None, None
        try:
            with self.ctx.db.transaction() as conn:
                conn.execute(
                    """INSERT INTO audit_log(action,entity_type,entity_id,details,user_id,username)
                       VALUES('print','invoice',?,?,?,?)""",
                    (int(invoice_id), channel, user_id, username),
                )
                row = conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action='print' AND entity_type='invoice' AND entity_id=?",
                    (int(invoice_id),),
                ).fetchone()
                count = int(row[0] if row is not None else 0)
            return count
        except Exception:
            return 0

    def _print_handler(self, invoice_id: int):
        async def handler(_):
            if self.native_files is None:
                self.notify("الطباعة الأصلية غير مهيأة في هذا البناء")
                return
            self.notify("جارٍ تحضير الطباعة...")
            try:
                html = self.ctx.documents.invoice_html(invoice_id)
                await self.native_files.print_html(html, name=f"nano-invoice-{invoice_id}")
                count = self._log_invoice_print(invoice_id, channel="print")
                if count and count > 1:
                    self.notify(f"طُبعت الفاتورة (مرة رقم {count})")
            except Exception as exc:
                self.notify(str(exc), kind="error")
        return handler

    def _pdf_handler(self, invoice_id: int):
        async def handler(_):
            if self.native_files is None:
                self.notify("تصدير PDF غير مهيأ في هذا البناء")
                return
            self.notify("جارٍ تجهيز ملف PDF...")
            try:
                html = self.ctx.documents.invoice_html(invoice_id)
                shared = await self.native_files.share_pdf(html, filename=f"nano_invoice_{invoice_id}.pdf")
                if shared:
                    self._log_invoice_print(invoice_id, channel="pdf")
                # share_pdf() returns False when the user simply dismissed the
                # native share sheet without picking an app -- without this,
                # that looked identical to "nothing happened" / "broken",
                # since neither an error nor any other feedback followed it.
                if not shared:
                    self.notify("تم إلغاء المشاركة")
            except Exception as exc:
                self.notify(str(exc), kind="error")
        return handler

    def _party_insight_text(self, is_sale: bool, party_id: int) -> str | None:
        """One-line activity insight for a selected party: last invoice
        recency, usual reorder rhythm and outstanding balance.

        Shared by the full-page editor and the new-invoice sheet so both
        surfaces show the same hint instead of two drifting copies.
        """
        repo = self.ctx.customers if is_sale else self.ctx.suppliers
        try:
            summary = repo.activity_summary(party_id)
        except Exception:
            return None
        count = int(summary.get("invoice_count") or 0)
        if count == 0:
            return None
        parts: list[str] = []
        last_date_obj: date | None = None
        last_date_str = summary.get("last_invoice_date")
        if last_date_str:
            try:
                last_date_obj = date.fromisoformat(str(last_date_str)[:10])
                days_since = (date.today() - last_date_obj).days
                if days_since <= 0:
                    parts.append("آخر فاتورة اليوم")
                elif days_since == 1:
                    parts.append("آخر فاتورة أمس")
                else:
                    parts.append(f"آخر فاتورة قبل {days_since} يوم")
            except Exception:
                last_date_obj = None
        recent_dates = sorted({
            r["invoice_date"] for r in (summary.get("recent_invoices") or []) if r.get("invoice_date")
        })
        if last_date_obj is not None and len(recent_dates) >= 2:
            try:
                parsed = [date.fromisoformat(str(d)[:10]) for d in recent_dates]
                intervals = [
                    (parsed[i + 1] - parsed[i]).days
                    for i in range(len(parsed) - 1)
                    if (parsed[i + 1] - parsed[i]).days > 0
                ]
                if intervals:
                    avg_days = round(sum(intervals) / len(intervals))
                    if avg_days >= 1:
                        next_expected = last_date_obj + timedelta(days=avg_days)
                        days_left = (next_expected - date.today()).days
                        if -2 <= days_left <= 3:
                            parts.append("اقترب موعد طلبه المعتاد")
                        elif days_left < -2:
                            parts.append("تجاوز موعد طلبه المعتاد")
            except Exception:
                pass
        outstanding = float(summary.get("outstanding_total") or 0)
        if outstanding > 1e-9:
            parts.append(f"مستحق عليه {self.money(outstanding)}")
        return " • ".join(parts) if parts else None

    def _sale_margin_warnings(self, inputs: list[InvoiceLineInput], item_map: dict[int, dict]) -> list[str]:
        """Margin-guard messages for a sale invoice's prepared lines.

        Extracted so the full-page editor's save and the new-invoice
        sheet's save share one rule (stored USD price per base unit vs the
        item's average cost) instead of two drifting copies.
        """
        warnings: list[str] = []
        for line in inputs:
            if not line.item_id:
                continue
            item = item_map.get(int(line.item_id))
            result = check_sale_margin(
                unit_price_usd=float(line.unit_price) / max(float(line.conversion_factor or 1), 1e-9),
                item=item,
                settings=self.ctx.settings,
            )
            if result.get("flag"):
                warnings.append(result.get("message") or "بيع تحت التكلفة")
        return warnings

    def _show_center_error(self, exc: Exception) -> None:
        """Render a recoverable error state instead of leaving a gray Flutter ErrorWidget."""
        if self.on_title_change:
            self.on_title_change("الفواتير", "تعذر تحميل شاشة الفواتير")
        message = str(exc).strip() or exc.__class__.__name__
        self.content.content = ft.Column(
            [
                ft.Text("الفواتير", size=24, weight=ft.FontWeight.BOLD),
                ft.Container(
                    ft.Column(
                        [
                            ft.Icon(ft.Icons.ERROR_OUTLINE, size=42, color=Colors.DANGER_DARKER),
                            ft.Text("تعذر تحميل شاشة الفواتير", size=18, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                "تم منع انهيار الواجهة. أعد المحاولة، وإذا تكرر الخطأ فاحتفظ بالتفاصيل التالية.",
                                size=12,
                                color=Colors.TEXT_SECONDARY,
                                text_align=ft.TextAlign.CENTER,
                            ),
                            ft.Text(message, size=11, color=Colors.DANGER_DARKER, selectable=True),
                            ft.FilledButton("إعادة المحاولة", icon=ft.Icons.REFRESH, on_click=lambda _: self.show_center()),
                        ],
                        spacing=10,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=20,
                    border=ft.border.all(1, Colors.DANGER_BORDER),
                    border_radius=14,
                    bgcolor=Colors.DANGER_BG,
                ),
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
        )
        self.page.update()

    def _bottom_sheet(self, *, icon: str, icon_color: str, icon_bg: str, title: str, subtitle: str, body: ft.Control) -> ft.BottomSheet:
        # Same modal shell used by SecurityCenter/LoginGate (drag handle,
        # rounded top corners, icon bubble + title/subtitle, LG shadow) --
        # kept identical so every "more options" surface in the app looks
        # like one system instead of a mix of AlertDialogs and sheets.
        sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)
        icon_bubble = ft.Container(ft.Icon(icon, color=icon_color, size=24), width=48, height=48, alignment=ft.alignment.center, bgcolor=icon_bg, border_radius=14)
        sheet.content = ft.Container(
            ft.Column(
                [
                    ft.Row([ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10)], alignment=ft.MainAxisAlignment.CENTER),
                    ft.Row(
                        [icon_bubble, ft.Column([ft.Text(title, size=18, weight=ft.FontWeight.BOLD), ft.Text(subtitle, size=11, color=Colors.TEXT_SECONDARY)], spacing=1, expand=True)],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Divider(height=1, color=Colors.BACKGROUND_ALT),
                    body,
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

    def _sheet_action(self, *, icon: str, icon_color: str, icon_bg: str, label: str, sublabel: str, on_click) -> ft.Container:
        return ft.Container(
            ft.Row(
                [
                    ft.Container(ft.Icon(icon, color=icon_color, size=20), width=40, height=40, alignment=ft.alignment.center, bgcolor=icon_bg, border_radius=12),
                    ft.Column([ft.Text(label, size=14, weight=ft.FontWeight.W_600), ft.Text(sublabel, size=11, color=Colors.TEXT_SECONDARY)], spacing=1, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_LEFT_ROUNDED, size=18, color=Colors.TEXT_FAINT),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=10, vertical=10),
            border_radius=12,
            ink=True,
            on_click=on_click,
        )

    def _invoice_more_dialog(self, invoice: dict) -> None:
        invoice_id = int(invoice["id"])
        sale = invoice.get("type") == "sale"
        party = invoice.get("party_name") or "نقدي"

        def close(_=None):
            self.page.close(sheet)

        async def do_print(_):
            close()
            # Same fix as the detail-sheet -> editor-sheet transition in
            # items_view.py: closing this BottomSheet and immediately
            # triggering more overlay activity (notify()'s SnackBar, here)
            # in the same synchronous tick races Flutter's dismiss
            # animation, so the SnackBar can silently never render. A short
            # yield lets the close animation finish first.
            await asyncio.sleep(0.1)
            await self._print_handler(invoice_id)(None)

        async def do_pdf(_):
            close()
            await asyncio.sleep(0.1)
            await self._pdf_handler(invoice_id)(None)

        def do_delete(_):
            close()
            self.confirm_delete(invoice_id)

        body = ft.Column(
            [
                self._sheet_action(icon=ft.Icons.PRINT_OUTLINED, icon_color=Colors.PRIMARY, icon_bg=Colors.PRIMARY_BG, label="طباعة", sublabel="طباعة الفاتورة مباشرة", on_click=do_print),
                self._sheet_action(icon=ft.Icons.DESCRIPTION_OUTLINED, icon_color=Colors.PRIMARY, icon_bg=Colors.PRIMARY_BG, label="مشاركة PDF", sublabel="تصدير الفاتورة كملف PDF", on_click=do_pdf),
                self._sheet_action(icon=ft.Icons.DELETE_OUTLINE, icon_color=Colors.DANGER, icon_bg=Colors.DANGER_BG, label="حذف الفاتورة", sublabel="حذف نهائي مع إعادة احتساب المخزون والأرصدة", on_click=do_delete),
            ],
            spacing=4,
        )
        sheet = self._bottom_sheet(
            icon=ft.Icons.RECEIPT_LONG_ROUNDED,
            icon_color=Colors.PRIMARY,
            icon_bg=Colors.PRIMARY_BG,
            title=f"فاتورة {'بيع' if sale else 'شراء'} #{invoice_id}",
            subtitle=party,
            body=body,
        )

    def show_invoice_detail(self, invoice_id: int) -> None:
        """Tap-to-open quick view for an invoice card -- same role as
        items_view.show_item_detail: a read-mostly bottom sheet (totals +
        line items) with print/PDF/edit reachable from a footer, so opening
        an invoice from the list no longer requires jumping straight into
        the full editor just to look at it.
        """
        invoice = self.ctx.invoices.get_invoice(invoice_id)
        if not invoice:
            self.notify("الفاتورة غير موجودة")
            return

        sale = invoice.get("type") == "sale"
        kind = "بيع" if sale else "شراء"
        party = invoice.get("party_name") or "نقدي"
        total = float(invoice.get("total") or 0)
        paid_amount = float(invoice.get("paid_amount") or 0)
        remaining = max(0.0, float(invoice.get("remaining_amount") or 0))
        status_key = invoice.get("payment_status") or ""
        status = STATUS_AR.get(status_key, status_key)
        status_bg = Colors.SUCCESS_BG if status_key == "paid" else Colors.WARNING_BG_ALT if status_key == "partial" else Colors.DANGER_BG
        status_fg = Colors.SUCCESS_DARKER if status_key == "paid" else Colors.ORANGE_DARK if status_key == "partial" else Colors.DANGER_DARKER
        accent = Colors.SUCCESS if sale else Colors.PURPLE
        accent_bg = Colors.SUCCESS_BG if sale else Colors.PURPLE_BG

        line_rows: list[ft.Control] = []
        units_cache: dict[int, list] = {}
        for line in invoice.get("lines") or []:
            qty = float(line.get("quantity") or 0)
            unit_price = float(line.get("unit_price") or 0)
            line_total = float(line.get("total") or 0)
            unit = line.get("unit_abbreviation") or line.get("unit_name") or ""
            name = line.get("item_name") or line.get("description") or "—"
            # Cross-unit equivalent (e.g. "≈ 6.67 صندوق" under a 10-meter
            # line): computed from the stored quantity_in_base divided by
            # each sibling unit's factor. Shown only when the item really
            # has more than one unit, so single-unit lines look exactly as
            # before. "=" marks the exact base-unit value, "≈" the rounded
            # sub-unit conversions.
            equiv_text = None
            try:
                line_item_id = int(line.get("item_id")) if line.get("item_id") is not None else None
            except (TypeError, ValueError):
                line_item_id = None
            if line_item_id is not None:
                if line_item_id not in units_cache:
                    try:
                        units_cache[line_item_id] = self.ctx.items.units(line_item_id)
                    except Exception:
                        units_cache[line_item_id] = []
                siblings = units_cache[line_item_id]
                if len(siblings) > 1:
                    try:
                        base_qty = float(line.get("quantity_in_base") or 0)
                    except (TypeError, ValueError):
                        base_qty = 0.0
                    try:
                        line_uid = int(line.get("unit_id")) if line.get("unit_id") is not None else None
                    except (TypeError, ValueError):
                        line_uid = None
                    parts = []
                    for u in siblings:
                        try:
                            uid = int(u.get("id"))
                        except (TypeError, ValueError):
                            continue
                        if uid == line_uid:
                            continue
                        try:
                            uf = float(u.get("conversion_factor") or 0)
                        except (TypeError, ValueError):
                            continue
                        if uf <= 0 or base_qty <= 0:
                            continue
                        mark = "=" if u.get("is_base") else "≈"
                        parts.append(f"{mark} {self._qty(base_qty / uf)} {u.get('name') or ''}".strip())
                    if parts:
                        equiv_text = "، ".join(parts)
            line_rows.append(
                ft.Container(
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text(name, size=12, weight=ft.FontWeight.W_600),
                                    ft.Row(
                                        [
                                            ft.Text(f"{self._qty(qty)} {unit} × ", size=10, color=Colors.TEXT_SECONDARY),
                                            money_row(unit_price, self.ctx.settings, size=10, color=Colors.TEXT_SECONDARY),
                                        ],
                                        spacing=0,
                                        tight=True,
                                    ),
                                ]
                                + ([ft.Text(equiv_text, size=9, color=Colors.TEXT_FAINT)] if equiv_text else []),
                                spacing=1, expand=True,
                            ),
                            money_row(line_total, self.ctx.settings, size=12, weight=ft.FontWeight.BOLD),
                        ],
                    ),
                    padding=8, bgcolor=Colors.BACKGROUND, border_radius=11,
                )
            )
        if not line_rows:
            line_rows = [ft.Text("لا توجد أسطر", size=11, color=Colors.TEXT_SECONDARY)]

        def close(_=None):
            self.page.close(sheet)

        async def edit(_=None):
            # Same close-then-yield-then-open fix used everywhere else in
            # this file/items_view -- closing this sheet and opening the
            # editor's sheet in the same tick can race Flutter's dismiss
            # animation and leave the editor invisible.
            close()
            await asyncio.sleep(0.1)
            self.show_editor(invoice_id)

        async def do_print(_):
            close()
            await asyncio.sleep(0.1)
            await self._print_handler(invoice_id)(None)

        async def do_pdf(_):
            close()
            await asyncio.sleep(0.1)
            await self._pdf_handler(invoice_id)(None)

        async def do_pay(_=None):
            # Same close-then-yield-then-open dance as _invoice_more_dialog:
            # firing another overlay in the same tick as this sheet's close
            # races Flutter's dismiss animation.
            close()
            await asyncio.sleep(0.1)
            self.show_payment_dialog(invoice_id)

        async def do_delete(_=None):
            close()
            await asyncio.sleep(0.1)
            self.confirm_delete(invoice_id)

        can_pay = remaining > 1e-9 and bool(invoice.get("customer_id") if sale else invoice.get("supplier_id"))

        export_row = []
        if self.native_files is not None:
            export_row = [
                ft.OutlinedButton("طباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=do_print, expand=True),
                ft.OutlinedButton("PDF", icon=ft.Icons.DESCRIPTION_OUTLINED, on_click=do_pdf, expand=True),
            ]

        body = ft.Column(
            [
                ft.Row([status_pill(status, status_fg, status_bg)], alignment=ft.MainAxisAlignment.START),
                metric_row([
                    kpi_card("الإجمالي", "", ft.Icons.RECEIPT_LONG_OUTLINED, Colors.PRIMARY, amount_usd=total, settings=self.ctx.settings),
                    kpi_card("المدفوع", "", ft.Icons.CHECK_CIRCLE_OUTLINE, Colors.SUCCESS, amount_usd=paid_amount, settings=self.ctx.settings),
                    kpi_card("المتبقي", "", ft.Icons.SCHEDULE, Colors.ORANGE if remaining > 1e-9 else Colors.SUCCESS, amount_usd=remaining, settings=self.ctx.settings),
                ], spacing=7),
                ft.Divider(height=1, color=Colors.BACKGROUND_ALT),
                ft.Text("الأصناف", size=13, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                ft.Column(line_rows, spacing=6),
                ft.Divider(height=1, color=Colors.BACKGROUND_ALT),
            ],
            spacing=12,
        )
        if export_row:
            body.controls.append(ft.Row(export_row, spacing=10))
        if can_pay:
            # The invoice table has no swipe gesture, so payment lives here
            # (and nowhere else) for outstanding invoices with a known
            # party -- one obvious place instead of a hidden shortcut.
            body.controls.append(
                ft.Row([ft.FilledButton("تسجيل دفعة", icon=ft.Icons.PAYMENTS_OUTLINED, on_click=do_pay, expand=True)], spacing=10)
            )
        actions: list[ft.Control] = [
            ft.OutlinedButton("تعديل", icon=ft.Icons.EDIT_OUTLINED, on_click=edit, expand=True),
            ft.FilledButton("إغلاق", on_click=close, expand=True),
        ]
        body.controls.append(ft.Row(actions, spacing=8))
        body.controls.append(
            ft.Row(
                [ft.TextButton("حذف الفاتورة", icon=ft.Icons.DELETE_OUTLINE, on_click=do_delete)],
                alignment=ft.MainAxisAlignment.CENTER,
            )
        )

        sheet = self._bottom_sheet(
            icon=ft.Icons.TRENDING_UP if sale else ft.Icons.TRENDING_DOWN,
            icon_color=accent,
            icon_bg=accent_bg,
            title=f"فاتورة {kind} #{invoice_id}",
            subtitle=f"{party} • {invoice.get('invoice_date') or '—'}",
            body=body,
        )

    def show_center(self) -> None:
        """Reference-style invoice browser: the same table language as the
        materials screen (round clipped grid, centred cells, tinted pinned
        header, zebra rows), with chip filters, KPI cards and the overdue
        alert kept on top of it."""
        if self.on_title_change:
            self.on_title_change("الفواتير", "المبيعات والمشتريات وحالات السداد")
        try:
            invoices = self.ctx.invoices.list_invoices(limit=250)
            search = SelectAllTextField(
                label="بحث في الفواتير",
                hint_text="رقم الفاتورة، العميل، المورد أو المرجع",
                prefix_icon=ft.Icons.SEARCH,
                border_radius=16,
                on_focus=lambda e: invoice_grid.disarm(),
            )
            cards = ft.Container(expand=True)
            filters = {"type": "all", "status": "all"}
            # Same reasoning as the items screen: build only a page of cards
            # at a time instead of all (up to 250) on every keystroke.
            render_limit = {"n": 60}
            type_row = ft.Row(spacing=6, wrap=True)
            status_row = ft.Row(spacing=6, wrap=True)

            outstanding = sum(max(0.0, float(i.get("remaining_amount") or 0)) for i in invoices)
            open_count = sum(1 for i in invoices if i.get("payment_status") != "paid")
            sales_total = sum(float(i.get("total") or 0) for i in invoices if i.get("type") == "sale")

            def days_since(invoice_date_str) -> int | None:
                if not invoice_date_str:
                    return None
                try:
                    return (date.today() - date.fromisoformat(str(invoice_date_str)[:10])).days
                except Exception:
                    return None

            overdue_threshold = invoice_settings.overdue_days(self.ctx.settings)
            overdue_invoices = [
                i for i in invoices
                if i.get("payment_status") != "paid"
                and (days_since(i.get("invoice_date")) or 0) > overdue_threshold
            ]
            overdue_total = sum(max(0.0, float(i.get("remaining_amount") or 0)) for i in overdue_invoices)

            def matches(inv: dict) -> bool:
                query = (search.value or "").strip().lower()
                if filters["type"] != "all" and inv.get("type") != filters["type"]:
                    return False
                if filters["status"] == "outstanding":
                    if inv.get("payment_status") not in {"unpaid", "partial"}:
                        return False
                elif filters["status"] != "all" and inv.get("payment_status") != filters["status"]:
                    return False
                if not query:
                    return True
                haystack = " ".join(
                    [
                        str(inv.get("id") or ""),
                        str(inv.get("party_name") or "نقدي"),
                        str(inv.get("reference") or ""),
                        str(inv.get("invoice_date") or ""),
                    ]
                ).lower()
                return query in haystack

            def _grid_formatter(col, value, row):
                if col.key == "id":
                    return f"#{int(row.get('id') or 0)}"
                if col.key == "type":
                    if row.get("type") == "sale":
                        return ("بيع", Colors.SUCCESS_DARKER, Colors.SUCCESS_BG)
                    return ("شراء", Colors.PURPLE, Colors.PURPLE_BG)
                if col.key == "party_name":
                    return str(value or "نقدي")
                if col.key == "payment_status":
                    status_key = str(row.get("payment_status") or "")
                    label = STATUS_AR.get(status_key, status_key)
                    remaining = max(0.0, float(row.get("remaining_amount") or 0))
                    days = days_since(row.get("invoice_date")) if remaining > 1e-9 else None
                    if days is not None and days > overdue_threshold:
                        return (f"{label} • متأخرة {days}ي", Colors.DANGER_DARKER, Colors.DANGER_BG)
                    if status_key == "paid":
                        return (label, Colors.SUCCESS_DARKER, Colors.SUCCESS_BG)
                    if status_key == "partial":
                        return (label, Colors.ORANGE_DARK, Colors.WARNING_BG_ALT)
                    return (label, Colors.DANGER_DARKER, Colors.DANGER_BG)
                if col.kind == "money":
                    return self.money(value)
                return None

            grid_sort = {"key": None, "dir": None}
            density_state = {"compact": self.ctx.settings.get("ui.invoices_density", "comfortable") == "compact"}
            selected_state = {"rows": []}
            grid_height = max(300, min(560, int((self.page.height or 760) * 0.52)))
            export_state = {"rows": []}
            hidden_state = {"keys": set(filter(None, self.ctx.settings.get("ui.invoices_hidden_columns", "").split(",")))}
            auto_hidden = {"keys": {"invoice_date"} if (self.page.width or 1200) < 720 else set()}

            def current_hidden() -> set[str]:
                return set(hidden_state["keys"]) | set(auto_hidden["keys"])

            def toggle_column(key: str) -> None:
                if key in hidden_state["keys"]:
                    hidden_state["keys"].discard(key)
                else:
                    hidden_state["keys"].add(key)
                self.ctx.settings.set("ui.invoices_hidden_columns", ",".join(sorted(hidden_state["keys"])))
                invoice_grid.set_hidden_keys(current_hidden())
                self.page.update()

            columns_button = ft.PopupMenuButton(
                icon=ft.Icons.VIEW_COLUMN,
                tooltip="إظهار/إخفاء الأعمدة",
                items=[
                    ft.PopupMenuItem(
                        text=c.label,
                        checked=c.key not in hidden_state["keys"],
                        on_click=lambda _, k=c.key: toggle_column(k),
                    )
                    for c in INVOICE_GRID_COLUMNS
                ],
            )

            async def export_csv(_=None) -> None:
                rows = export_state["rows"]
                if not rows:
                    self.notify("لا توجد بيانات للتصدير", kind="warning")
                    return
                if self.native_files is None:
                    self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                    return
                try:
                    path = Path(tempfile.gettempdir()) / f"nano_invoices_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                        writer = _csv.writer(fh)
                        writer.writerow(["الفاتورة", "النوع", "الطرف", "التاريخ", "الإجمالي", "المدفوع", "المتبقي", "الحالة", "المرجع"])
                        for inv in rows:
                            writer.writerow([
                                inv.get("id") or "",
                                "بيع" if inv.get("type") == "sale" else "شراء",
                                inv.get("party_name") or "نقدي",
                                inv.get("invoice_date") or "",
                                inv.get("total") or 0,
                                inv.get("paid_amount") or 0,
                                max(0.0, float(inv.get("remaining_amount") or 0)),
                                STATUS_AR.get(str(inv.get("payment_status") or ""), inv.get("payment_status") or ""),
                                inv.get("reference") or "",
                            ])
                    await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير الفواتير")
                    self.notify("تم تجهيز ملف CSV (بترميز يفتح في Excel العربي)")
                except Exception as exc:
                    self.notify(str(exc), kind="error")

            export_button = ft.IconButton(
                icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
                tooltip="تصدير CSV للعرض الحالي",
                on_click=export_csv,
            )

            def focus_search() -> None:
                invoice_grid.disarm()
                try:
                    search.focus()
                except Exception:
                    pass

            def invoice_is_overdue(row: dict) -> bool:
                remaining = max(0.0, float(row.get("remaining_amount") or 0))
                days = days_since(row.get("invoice_date"))
                return remaining > 1e-9 and days is not None and days > overdue_threshold

            def invoice_row_bg(row: dict) -> str | None:
                return Colors.DANGER_BG if invoice_is_overdue(row) else None

            def invoice_group(row: dict) -> str | None:
                if grid_sort["key"]:
                    return None
                if invoice_is_overdue(row):
                    return "متأخرة السداد"
                days = days_since(row.get("invoice_date"))
                if days is None:
                    return "بدون تاريخ"
                if days <= 0:
                    return "اليوم"
                if days == 1:
                    return "أمس"
                if days <= 7:
                    return "هذا الأسبوع"
                if days <= 30:
                    return "هذا الشهر"
                return "أقدم من شهر"

            def detail_chip(icon, label: str, value: str) -> ft.Control:
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

            def invoice_expand_panel(row: dict) -> ft.Control:
                chips = [
                    detail_chip(ft.Icons.TAG_OUTLINED, "المرجع", str(row.get("reference") or "—")),
                    detail_chip(
                        ft.Icons.PAYMENTS_OUTLINED, "مدفوع",
                        f"{self.money(row.get('paid_amount'))} من {self.money(row.get('total'))}",
                    ),
                ]
                notes = str(row.get("notes") or "").strip()
                if notes:
                    chips.append(detail_chip(ft.Icons.NOTES_OUTLINED, "ملاحظات", notes[:80]))
                return ft.Row(chips, spacing=8, wrap=True)

            def duplicate_invoice(invoice_id: int) -> None:
                src = self.ctx.invoices.get_invoice(invoice_id)
                if not src:
                    return
                lines = [
                    {"item_id": ln.get("item_id"), "quantity": ln.get("quantity"), "unit_price": ln.get("unit_price")}
                    for ln in (src.get("lines") or [])
                    if ln.get("item_id")
                ]
                skipped = len(src.get("lines") or []) - len(lines)
                party = src.get("customer_id") if src.get("type") == "sale" else src.get("supplier_id")
                self.show_editor(None, src.get("type"), prefill_lines=lines, party_id=party)
                if skipped:
                    self.notify(f"تم تجاهل {skipped} بند بلا مادة أثناء التكرار", kind="warning")

            def apply_grid_sort(key: str | None, direction: str | None) -> None:
                grid_sort["key"], grid_sort["dir"] = key, direction
                render_limit["n"] = 60
                refresh_cards()

            def row_menu(row: dict) -> list:
                invoice_id = int(row.get("id") or 0)
                remaining = max(0.0, float(row.get("remaining_amount") or 0))
                items: list = [
                    ("فتح", ft.Icons.OPEN_IN_NEW_ROUNDED, lambda i=invoice_id: self.show_invoice_detail(i)),
                    ("تعديل", ft.Icons.EDIT_OUTLINED, lambda i=invoice_id: self.show_editor(i)),
                    ("تكرار كفاتورة جديدة", ft.Icons.CONTENT_COPY_OUTLINED, lambda i=invoice_id: duplicate_invoice(i)),
                ]
                if remaining > 1e-9:
                    items.append(("تسجيل دفعة", ft.Icons.PAYMENTS_OUTLINED, lambda i=invoice_id: self.show_payment_dialog(i)))
                items.extend([
                    ("طباعة", ft.Icons.PRINT_OUTLINED, self._print_handler(invoice_id)),
                    ("مشاركة PDF", ft.Icons.PICTURE_AS_PDF, self._pdf_handler(invoice_id)),
                    None,
                    ("حذف", ft.Icons.DELETE_OUTLINE, lambda i=invoice_id: self.confirm_delete(i)),
                ])
                return items

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
                            ft.FilledButton(
                                "حذف المحدد", icon=ft.Icons.DELETE_OUTLINE,
                                style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE),
                                on_click=confirm_bulk_delete,
                            ),
                            ft.TextButton("إلغاء التحديد", on_click=lambda _: invoice_grid.clear_selection()),
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
                dialog = ft.AlertDialog(modal=True, title=ft.Text("تأكيد حذف الفواتير"))

                def yes(_ev=None) -> None:
                    self.page.close(dialog)
                    deleted = 0
                    failures = 0
                    for row in rows:
                        try:
                            self.ctx.invoices.delete_invoice(int(row.get("id") or 0))
                            deleted += 1
                        except Exception:
                            failures += 1
                    self.show_center()
                    if failures:
                        self.notify(f"حُذفت {deleted} فاتورة، وتعذّر حذف {failures}", kind="warning")
                    else:
                        self.notify(f"حُذفت {deleted} فاتورة", sound_kind="success")

                def no(_ev=None) -> None:
                    self.page.close(dialog)

                dialog.content = ft.Text(
                    f"سيتم حذف {len(rows)} فاتورة محددة نهائيًا. سيُعاد احتساب الحسابات والمخزون تلقائيًا."
                )
                dialog.actions = [
                    ft.TextButton("إلغاء", on_click=no),
                    ft.FilledButton("حذف", on_click=yes, style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE)),
                ]
                self.page.open(dialog)

            def toggle_density(_=None) -> None:
                compact = not density_state["compact"]
                density_state["compact"] = compact
                invoice_grid.set_row_height(36 if compact else 48)
                density_button.icon = ft.Icons.DENSITY_SMALL if compact else ft.Icons.DENSITY_MEDIUM
                density_button.tooltip = "عرض مريح" if compact else "عرض مضغوط"
                self.ctx.settings.set("ui.invoices_density", "compact" if compact else "comfortable")
                self.page.update()

            density_button = ft.IconButton(
                icon=ft.Icons.DENSITY_SMALL if density_state["compact"] else ft.Icons.DENSITY_MEDIUM,
                tooltip="عرض مريح" if density_state["compact"] else "عرض مضغوط",
                on_click=toggle_density,
            )
            bulk_bar = ft.Container(visible=False)

            # Same table language as the materials screen: round clipped
            # card, tinted pinned header, zebra rows, centred cells. Rows are
            # display-only on purpose -- an invoice is a financial record, so
            # nothing is edited in place; the row's open icon (or a row tap)
            # leads to the detail sheet, which carries payment/print/PDF/
            # edit/delete. The bounded viewport keeps the header pinned even
            # though the invoice screen itself scrolls as one page.
            invoice_grid = DataGrid(
                columns=INVOICE_GRID_COLUMNS,
                rows=[],
                page=self.page,
                row_height=36 if density_state["compact"] else 48,
                viewport_height=grid_height,
                hidden_keys=current_hidden(),
                on_row_activate=lambda row: self.show_invoice_detail(int(row.get("id") or 0)),
                on_sort=apply_grid_sort,
                selectable=True,
                on_selection_change=render_bulk_bar,
                on_row_menu=row_menu,
                row_bg=invoice_row_bg,
                group_by=invoice_group,
                on_row_expand=invoice_expand_panel,
                on_search_shortcut=focus_search,
                on_new_shortcut=lambda: self.show_editor(None, "sale"),
                formatter=_grid_formatter,
                boxed=True,
                center_cells=True,
                activate_tooltip="فتح الفاتورة",
                empty_text="لا توجد فواتير",
            )
            grid_more = ft.Container(visible=False)
            desktop_list = ft.Column(
                [bulk_bar, ft.Container(invoice_grid, expand=True), grid_more],
                spacing=8, expand=True,
            )

            def refresh_cards(_=None):
                matched = [inv for inv in invoices if matches(inv)]
                if grid_sort["key"]:
                    sort_key = grid_sort["key"]
                    reverse = grid_sort["dir"] == "desc"

                    def sort_value(inv):
                        if sort_key in ("id", "total", "remaining_amount"):
                            try:
                                return float(inv.get(sort_key) or 0)
                            except (TypeError, ValueError):
                                return 0.0
                        return str(inv.get(sort_key) or "")

                    matched.sort(key=sort_value, reverse=reverse)
                else:
                    # Default view: overdue invoices pinned first, the rest
                    # stay in the service's date-descending order, and the
                    # group banners narrate "متأخرة السداد / اليوم / أمس…".
                    matched.sort(key=lambda inv: 0 if invoice_is_overdue(inv) else 1)
                export_state["rows"] = matched
                invoice_grid.set_hidden_keys(current_hidden())
                invoice_grid.set_group_by(None if grid_sort["key"] else invoice_group)
                shown = matched[: render_limit["n"]]
                if not shown:
                    invoice_grid.set_footer(None)
                    if invoices:
                        cards.content = empty_state(
                            "لا توجد فواتير مطابقة",
                            icon=ft.Icons.RECEIPT_LONG_OUTLINED,
                            hint="جرّب تغيير البحث أو عوامل التصفية",
                        )
                    else:
                        cards.content = empty_state(
                            "لا توجد فواتير بعد",
                            icon=ft.Icons.RECEIPT_LONG_OUTLINED,
                            hint="أنشئ أول فاتورة بيع أو شراء",
                            action_label="بيع جديد",
                            on_action=lambda _: self.show_editor(None, "sale"),
                        )
                    self.page.update()
                    return
                invoice_grid.set_rows(shown)
                # Totals cover the whole filtered set, not just the first
                # page of 60 -- so the footer answers "how much is open in
                # what I'm looking at" without paging through 250 rows.
                invoice_grid.set_footer({
                    "type": f"{len(matched)} فاتورة",
                    "total": self.money(sum(float(i.get("total") or 0) for i in matched)),
                    "remaining_amount": self.money(sum(max(0.0, float(i.get("remaining_amount") or 0)) for i in matched)),
                })
                remaining = len(matched) - len(shown)
                if remaining > 0:
                    grid_more.content = ft.Row(
                        [ft.OutlinedButton(f"تحميل المزيد ({remaining})", icon=ft.Icons.EXPAND_MORE_ROUNDED,
                                           on_click=lambda _=None: (render_limit.__setitem__("n", render_limit["n"] + 60), refresh_cards()))],
                        alignment=ft.MainAxisAlignment.CENTER,
                    )
                    grid_more.visible = True
                else:
                    grid_more.visible = False
                cards.content = desktop_list
                self.page.update()

            def refresh_from_search(e=None):
                render_limit["n"] = 60
                refresh_cards()

            def chip(text: str, key: str, value: str, row: ft.Row):
                active = filters[key] == value
                return ft.Container(
                    ft.Text(text, size=11, color=Colors.WHITE if active else Colors.TEXT_MUTED, weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_500),
                    padding=ft.padding.symmetric(horizontal=12, vertical=8),
                    bgcolor=Colors.PRIMARY if active else Colors.WHITE,
                    border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
                    border_radius=14,
                    on_click=lambda _: set_filter(key, value), ink=True,
                )

            def render_filters() -> None:
                type_row.controls = [chip("الكل", "type", "all", type_row), chip("مبيعات", "type", "sale", type_row), chip("مشتريات", "type", "purchase", type_row)]
                status_row.controls = [
                    chip("كل الحالات", "status", "all", status_row),
                    chip("المستحق", "status", "outstanding", status_row),
                    chip("غير مدفوعة", "status", "unpaid", status_row),
                    chip("جزئية", "status", "partial", status_row),
                    chip("مدفوعة", "status", "paid", status_row),
                ]

            def set_filter(key: str, value: str) -> None:
                filters[key] = value
                render_limit["n"] = 60
                render_filters()
                refresh_cards()

            search.on_change = refresh_from_search
            render_filters()
            refresh_cards()

            # --- Sticky header: search + filters stay put while the list
            # below scrolls, so they're never lost mid-scroll on a long
            # invoice list. ---
            sticky_header = ft.Container(
                ft.Column(
                    [
                        ft.Row(
                            [ft.Container(search, expand=True), columns_button, export_button, density_button],
                            spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Column([ft.Text("النوع", size=10, color=Colors.TEXT_SECONDARY), type_row], spacing=4),
                        ft.Column([ft.Text("حالة السداد", size=10, color=Colors.TEXT_SECONDARY), status_row], spacing=4),
                    ],
                    spacing=10,
                ),
                padding=ft.padding.only(left=16, right=16, top=14, bottom=12),
                bgcolor=Colors.WHITE,
                border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
                shadow=Shadow.SM,
            )

            # --- Scrollable middle: KPI cards, overdue alert, invoice list. ---
            scroll_body = ft.Column(
                [
                    metric_row([
                        kpi_card("إجمالي المبيعات", self.money(sales_total), ft.Icons.TRENDING_UP, Colors.SUCCESS, on_tap=lambda _: set_filter("type", "sale")),
                        kpi_card("المستحق", self.money(outstanding), ft.Icons.SCHEDULE, Colors.ORANGE, on_tap=lambda _: set_filter("status", "outstanding")),
                        kpi_card("فواتير مفتوحة", str(open_count), ft.Icons.RECEIPT_LONG_OUTLINED, Colors.PRIMARY, on_tap=lambda _: set_filter("status", "outstanding")),
                    ], spacing=8),
                    ft.Container(
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.ERROR_OUTLINE, size=16, color=Colors.DANGER_DARKER),
                                ft.Row(
                                    [
                                        ft.Text(
                                            f"{len(overdue_invoices)} فاتورة متأخرة السداد أكثر من 30 يومًا بقيمة ",
                                            size=11, color=Colors.DANGER_DARKER,
                                        ),
                                        ft.Text(self.money(overdue_total), size=11, color=Colors.DANGER_DARKER),
                                    ],
                                    spacing=0,
                                    tight=True,
                                    expand=True,
                                ),
                            ],
                            spacing=8,
                        ),
                        padding=ft.padding.symmetric(horizontal=12, vertical=9),
                        bgcolor=Colors.DANGER_BG, border=ft.border.all(1, Colors.DANGER_BORDER), border_radius=12,
                        visible=bool(overdue_invoices),
                    ),
                    cards,
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            )

            # New sale/purchase invoices are created from the shell's ribbon
            # (فاتورة بيع / فاتورة شراء); the old floating speed-dial was
            # mobile chrome.
            self.content.content = ft.Column([sticky_header, scroll_body], spacing=0, expand=True)
            self.page.update()
        except Exception as exc:
            self._show_center_error(exc)

    def show_payment_dialog(self, invoice_id: int) -> None:
        invoice = self.ctx.invoices.get_invoice(invoice_id)
        if not invoice:
            self.notify("الفاتورة غير موجودة")
            return
        remaining = max(0.0, float(invoice["remaining_amount"] or 0))
        if remaining <= 1e-9:
            self.notify("الفاتورة مسددة بالكامل")
            return

        amount = SmartAmountField(
            label=currency.amount_field_label("المبلغ", self.ctx.settings),
            value=currency.to_input_text(remaining, self.ctx.settings),
        )
        payment_date = SmartDateField(label="التاريخ", value=date.today().isoformat())
        reference = SelectAllTextField(label="المرجع")
        notes = SelectAllTextField(label="ملاحظات", multiline=True, min_lines=1, max_lines=3)
        sheet = new_form_sheet()

        def close(_=None):
            self.page.close(sheet)

        def save(_):
            try:
                voucher_id = self.ctx.payments.register_invoice_payment(
                    invoice_id,
                    currency.parse_display_input(amount.value, self.ctx.settings),
                    payment_date=payment_date.value,
                    reference=reference.value,
                    notes=notes.value,
                )
                close()
                self.notify(f"تم تسجيل الدفعة بسند #{voucher_id}", sound_kind="save")
                self.show_center()
                if self.on_saved:
                    self.on_saved()
            except Exception as exc:
                self.notify(str(exc), kind="error")

        render_form_sheet(
            self.page, sheet,
            title=f"تسجيل دفعة — فاتورة #{invoice_id}",
            fields=[
                ft.Row(
                    [
                        ft.Text(f"{invoice.get('party_name') or '—'} • المتبقي ", color=Colors.TEXT_SECONDARY),
                        ft.Text(self.money(remaining), color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=0,
                    tight=True,
                ),
                amount, payment_date, reference, notes,
            ],
            on_close=close, on_save=save,
            save_label="تسجيل", save_icon=ft.Icons.PAYMENTS_OUTLINED,
        )
        self.page.open(sheet)

    def confirm_delete(self, invoice_id: int) -> None:
        invoice = self.ctx.invoices.get_invoice(invoice_id)
        if not invoice:
            self.notify("الفاتورة غير موجودة")
            return

        dialog = ft.AlertDialog(modal=True)

        def close(_=None):
            self.page.close(dialog)

        def do_delete(_):
            try:
                self.ctx.invoices.delete_invoice(invoice_id)
                self.page.close(dialog)
                self.notify(f"تم حذف الفاتورة #{invoice_id} وعكس آثارها المحاسبية والمخزنية", kind="success", sound_kind="delete")
                self.show_center()
                if self.on_saved:
                    self.on_saved()
            except Exception as exc:
                self.page.close(dialog)
                self.notify(str(exc), kind="error")

        dialog.title = ft.Text("تأكيد حذف الفاتورة")
        dialog.content = ft.Text(
            f"سيتم حذف الفاتورة #{invoice_id} وإعادة احتساب المخزون والأرصدة والتكلفة. لا يمكن التراجع عن العملية."
        )
        dialog.actions = [
            ft.TextButton("إلغاء", on_click=close),
            ft.FilledButton("حذف نهائي", icon=ft.Icons.DELETE_FOREVER, on_click=do_delete),
        ]
        self.page.open(dialog)

    def show_new_invoice_sheet(
        self,
        invoice_type: str | None = None,
        *,
        prefill_lines: list[dict] | None = None,
        party_id: int | None = None,
    ) -> None:
        """Create a sale/purchase invoice through a 3-step bottom sheet.

        Step 1 (بيانات): type, party -- with the same one-line activity
        insight the page editor shows -- date and reference. Step 2 (بنود):
        the same editable DataGrid used by the bulk-entry sheet and the
        materials screen (picker column, live count/total footer, barcode
        scan). Step 3 (الدفع): paid amount with quick chips, notes and a
        live remaining summary.

        Saving deliberately reuses the page editor's exact rules: the same
        ``InvoiceLineInput`` shape (unit_price stored in USD), the same
        margin guard (including its two-attempt confirm) and the same
        ``create_invoice`` call, so an invoice saved from this sheet is
        indistinguishable from one saved from the full page.
        """
        invoice_type = invoice_type if invoice_type in ("sale", "purchase") else "sale"
        self.invoice_type = invoice_type
        settings = self.ctx.settings
        rate = currency.get_effective_rate(settings)
        customers = self.ctx.customers.list()
        suppliers = self.ctx.suppliers.list()
        items = self.ctx.items.list()
        item_map = {int(i["id"]): i for i in items}
        previous_key_handler = getattr(self.page, "on_keyboard_event", None)
        step = {"idx": 0, "opened": False}

        def _safe_update(control) -> None:
            # A control may not be attached to the page yet on the first
            # render -- same guard the page editor uses.
            try:
                control.update()
            except Exception:
                pass

        # ------------------------------------------------------------- header
        type_dd = SegmentedToggle(
            options=[
                SegmentOption("sale", "بيع", ft.Icons.TRENDING_UP),
                SegmentOption("purchase", "شراء", ft.Icons.TRENDING_DOWN),
            ],
            value=invoice_type,
        )
        party_dd = SearchSelect(label="العميل")
        party_insight = ft.Container(visible=False)
        invoice_date = SmartDateField(label="التاريخ", value=date.today().isoformat())
        reference = SelectAllTextField(label="المرجع")

        def party_options() -> None:
            is_sale = (type_dd.value or "sale") == "sale"
            source = customers if is_sale else suppliers
            party_dd.label = "العميل" if is_sale else "المورد"
            party_dd.set_choices([(str(p["id"]), p["name"]) for p in source])
            if party_dd.value and not any(str(p["id"]) == party_dd.value for p in source):
                party_dd.value = None

        def update_party_insight() -> None:
            is_sale = (type_dd.value or "sale") == "sale"
            text = self._party_insight_text(is_sale, int(party_dd.value)) if party_dd.value else None
            if not text:
                party_insight.visible = False
                _safe_update(party_insight)
                return
            party_insight.content = ft.Row(
                [
                    ft.Icon(ft.Icons.LIGHTBULB_OUTLINE, size=15, color=Colors.PRIMARY),
                    ft.Text(text, size=11, color=Colors.PRIMARY_DARK, expand=True),
                ],
                spacing=8,
            )
            party_insight.padding = ft.padding.symmetric(horizontal=10, vertical=8)
            party_insight.bgcolor = Colors.PRIMARY_BG
            party_insight.border = ft.border.all(1, Colors.PRIMARY_BORDER)
            party_insight.border_radius = 10
            party_insight.visible = True
            _safe_update(party_insight)

        # --------------------------------------------------------------- grid
        def default_display_price(item_row: dict) -> float:
            price_key = "selling_price" if (type_dd.value or "sale") == "sale" else "purchase_price"
            return round(currency.to_display(float(item_row.get(price_key) or 0), rate), 2)

        def row_formatter(col, value, row):
            if col.key == "item":
                if not value:
                    return "— اختر مادة —"
                item_row = item_map.get(int(value))
                return item_row["name"] if item_row else "—"
            if col.key == "price":
                return currency.format_plain(value) if value else ""
            return None

        lines_grid = DataGrid(
            columns=[
                GridColumn("item", "المادة", editable=True, kind="picker"),
                GridColumn("qty", "الكمية", editable=True, kind="number", width=100, align="end"),
                GridColumn("price", currency.amount_field_label("السعر", settings), editable=True, kind="money", width=150, align="end"),
            ],
            rows=[], page=self.page,
            picker_options=lambda _col, _row: [
                (int(item_row["id"]), f"{item_row['name']} · {item_row.get('barcode') or 'بلا باركود'}")
                for item_row in items
            ],
            on_cell_commit=lambda index, key, value: commit_sheet_line_cell(index, key, value),
            formatter=row_formatter,
            empty_text="اضغط «سطر جديد» أو «مسح باركود»",
        )

        def add_blank(_=None) -> None:
            lines_grid.add_row({"qty": 1})

        def add_or_bump_row(item_id: int) -> str:
            for index, row in enumerate(lines_grid.rows):
                if row.get("item") and int(row["item"]) == item_id:
                    try:
                        qty = float(row.get("qty") or 0) + 1
                    except (TypeError, ValueError):
                        qty = 1.0
                    lines_grid.set_cell(index, "qty", qty)
                    refresh_summary()
                    return "bumped"
            item_row = item_map.get(item_id)
            index = lines_grid.add_row({"qty": 1})
            lines_grid.set_cell(index, "item", item_id)
            if item_row is not None:
                lines_grid.set_cell(index, "price", default_display_price(item_row))
            refresh_summary()
            return "added"

        async def scan_line(_=None) -> None:
            if self.native_files is None:
                self.notify("مسح الباركود غير مهيأ في هذا البناء", kind="error")
                return
            try:
                code = await self.native_files.scan_barcode()
            except Exception as exc:
                self.notify(str(exc), kind="error")
                return
            if not code:
                return
            found = self.ctx.items.find_by_barcode(code)
            if not found:
                self.notify("لا توجد مادة بهذا الباركود", kind="error", sound_kind="barcode_error")
                return
            status = add_or_bump_row(int(found["id"]))
            if barcode_settings.scan_feedback_mode(settings) == "brief":
                self.notify("✔", kind="success", sound_kind="scan")
            elif status == "bumped":
                self.notify(f"✔ زيدت الكمية: {found['name']}", kind="success", sound_kind="scan")
            else:
                self.notify(f"✔ أُضيف: {found['name']}", kind="success", sound_kind="scan")

        # ------------------------------------------------- payment / summary
        paid = SmartAmountField(
            label=currency.amount_field_label("الدفعة الأولى", settings),
            value="0",
            on_change=lambda e: refresh_summary(),
        )
        notes = SelectAllTextField(label="ملاحظات", multiline=True, min_lines=1, max_lines=3)
        lines_summary_text = ft.Text("لا بنود بعد", size=10, color=Colors.TEXT_SECONDARY)
        total_text = ft.Text("0", size=20, weight=ft.FontWeight.BOLD)
        remaining_text = ft.Text("0", size=16, weight=ft.FontWeight.BOLD)
        paid_progress = ft.ProgressBar(value=0, color=Colors.SUCCESS, bgcolor=Colors.BACKGROUND_ALT, height=6, border_radius=6)

        def rows_total_display() -> float:
            total = 0.0
            for row in lines_grid.rows:
                if not row.get("item"):
                    continue
                try:
                    total += float(row.get("qty") or 0) * float(row.get("price") or 0)
                except (TypeError, ValueError):
                    continue
            return total

        def refresh_summary(_=None) -> None:
            total = rows_total_display()
            count = sum(1 for row in lines_grid.rows if row.get("item"))
            lines_grid.set_footer(
                {"item": f"{count} بند", "price": currency.format_plain(round(total, 2))} if count else None
            )
            lines_summary_text.value = f"{count} بند" if count else "لا بنود بعد"
            # Anonymous invoices are cash by definition (same rule the page
            # editor and the accounting service enforce): with no party the
            # paid field auto-fills with the total and locks.
            cash_without_party = not bool(party_dd.value)
            paid.disabled = cash_without_party
            paid.label = "مدفوع نقدًا (تلقائي)" if cash_without_party else currency.amount_field_label("الدفعة الأولى", settings)
            if cash_without_party:
                paid.value = currency.format_plain(total)
                paid_value = total
            else:
                try:
                    paid_value = float(paid.value or 0)
                except Exception:
                    paid_value = 0.0
            total_text.value = currency.format_display_value(total, settings)
            remaining_text.value = currency.format_display_value(max(0.0, total - paid_value), settings)
            remaining_text.color = Colors.SUCCESS if total - paid_value <= 1e-9 else Colors.ORANGE
            paid_progress.value = max(0.0, min(1.0, paid_value / total)) if total > 1e-9 else 1.0
            quick_chips.visible = not cash_without_party
            _safe_update(lines_summary_text)
            _safe_update(paid)
            _safe_update(total_text)
            _safe_update(remaining_text)
            _safe_update(paid_progress)
            _safe_update(quick_chips)

        def commit_sheet_line_cell(index: int, key: str, value) -> None:
            if key == "item" and value:
                row = lines_grid.row_at(index) or {}
                if not row.get("price"):
                    item_row = item_map.get(int(value))
                    if item_row is not None:
                        lines_grid.set_cell(index, "price", default_display_price(item_row), notify=False)
            refresh_summary()

        def quick_pay(ratio: float) -> None:
            paid.value = currency.format_plain(round(rows_total_display() * ratio, 2))
            _safe_update(paid)
            refresh_summary()

        def quick_pay_chip(label: str, ratio: float, icon) -> ft.Container:
            return ft.Container(
                ft.Row([ft.Icon(icon, size=14, color=Colors.PRIMARY), ft.Text(label, size=11, weight=ft.FontWeight.W_600)], spacing=5),
                padding=ft.padding.symmetric(horizontal=10, vertical=7),
                border=ft.border.all(1, Colors.PRIMARY), border_radius=12,
                bgcolor=Colors.WHITE, ink=True,
                on_click=lambda _: quick_pay(ratio),
            )

        quick_chips = ft.Row(
            [
                quick_pay_chip("دفعة كاملة", 1.0, ft.Icons.CHECK_CIRCLE_OUTLINE),
                quick_pay_chip("النصف", 0.5, ft.Icons.PERCENT),
                quick_pay_chip("بدون دفعة", 0.0, ft.Icons.MONEY_OFF_OUTLINED),
            ],
            spacing=6, wrap=True,
        )

        # --------------------------------------------------------------- draft
        # An unfinished invoice survives an accidental dismiss (back gesture,
        # drag-down): its state is stored in settings and offered back next
        # time the same invoice type is being created.
        draft_key = "ui.invoice_draft"
        draft_saved = {"saved": False}

        def clear_draft() -> None:
            try:
                self.ctx.settings.set(draft_key, "")
            except Exception:
                pass

        def load_draft() -> dict | None:
            try:
                raw = self.ctx.settings.get(draft_key, "")
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict) and data.get("lines") is not None:
                        return data
            except Exception:
                pass
            return None

        def persist_draft() -> None:
            if draft_saved["saved"]:
                return
            lines = []
            for row in lines_grid.rows:
                if not row.get("item"):
                    continue
                lines.append({"item_id": int(row["item"]), "qty": row.get("qty"), "price": row.get("price")})
            if not lines and not party_dd.value and not (reference.value or "").strip() and not (notes.value or "").strip():
                clear_draft()
                return
            payload = {
                "type": type_dd.value or "sale",
                "party_id": int(party_dd.value) if party_dd.value else None,
                "invoice_date": invoice_date.value,
                "reference": reference.value,
                "notes": notes.value,
                "paid": paid.value,
                "lines": lines,
            }
            try:
                self.ctx.settings.set(draft_key, json.dumps(payload, ensure_ascii=False))
            except Exception:
                pass

        def restore_draft(draft: dict) -> None:
            type_dd.value = draft.get("type") if draft.get("type") in ("sale", "purchase") else "sale"
            self.invoice_type = type_dd.value
            party_options()
            if draft.get("party_id"):
                try:
                    party_dd.value = str(int(draft["party_id"]))
                except Exception:
                    pass
            invoice_date.value = draft.get("invoice_date") or date.today().isoformat()
            reference.value = draft.get("reference") or ""
            notes.value = draft.get("notes") or ""
            paid.value = str(draft.get("paid") or "0")
            lines_grid.set_rows([])
            for line in draft.get("lines") or []:
                try:
                    item_id = int(line.get("item_id"))
                except (TypeError, ValueError):
                    continue
                item_row = item_map.get(item_id)
                if item_row is None:
                    continue
                index = lines_grid.add_row({"qty": line.get("qty") or 1})
                lines_grid.set_cell(index, "item", item_id)
                if line.get("price") not in (None, ""):
                    lines_grid.set_cell(index, "price", float(line["price"]))
                else:
                    lines_grid.set_cell(index, "price", default_display_price(item_row))
            update_party_insight()
            refresh_summary()

        draft_banner = ft.Container(visible=False)

        def hide_draft_banner() -> None:
            draft_banner.visible = False
            _safe_update(draft_banner)

        def show_draft_banner(draft: dict) -> None:
            draft_banner.content = ft.Row(
                [
                    ft.Icon(ft.Icons.RESTORE_ROUNDED, size=16, color=Colors.ORANGE_DARK),
                    ft.Text("توجد مسودة غير محفوظة لهذه الفاتورة", size=11, color=Colors.ORANGE_DARK, expand=True),
                    ft.TextButton("استعادة", on_click=lambda _: (restore_draft(draft), hide_draft_banner())),
                    ft.TextButton("حذف", on_click=lambda _: (clear_draft(), hide_draft_banner())),
                ],
                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            draft_banner.padding = ft.padding.symmetric(horizontal=10, vertical=6)
            draft_banner.bgcolor = Colors.WARNING_BG_ALT
            draft_banner.border = ft.border.all(1, Colors.WARNING)
            draft_banner.border_radius = 10
            draft_banner.visible = True
            _safe_update(draft_banner)

        # ---------------------------------------------------------------- save
        def _default_unit(item_id: int) -> tuple[int | None, float]:
            try:
                units = self.ctx.items.units(item_id)
            except Exception:
                units = []
            unit = _default_invoice_unit(units)
            if unit is None:
                return None, 1.0
            try:
                return int(unit["id"]), float(unit.get("conversion_factor") or 1)
            except Exception:
                return None, 1.0

        def build_inputs() -> list[InvoiceLineInput]:
            inputs: list[InvoiceLineInput] = []
            for idx, row in enumerate(lines_grid.rows, start=1):
                if not row.get("item"):
                    continue
                item_id = int(row["item"])
                try:
                    qty = float(row.get("qty") or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                if qty <= 0:
                    raise ValueError(f"كمية البند {idx} غير صحيح")
                try:
                    unit_price = (
                        currency.parse_display_input(str(row.get("price")), settings)
                        if row.get("price") not in (None, "")
                        else 0.0
                    )
                except Exception as exc:
                    raise ValueError(f"سعر البند {idx} غير صحيح") from exc
                unit_id, factor = _default_unit(item_id)
                inputs.append(
                    InvoiceLineInput(
                        description="",
                        item_id=item_id,
                        unit_id=unit_id,
                        conversion_factor=factor,
                        quantity=qty,
                        unit_price=unit_price,
                    )
                )
            if not inputs:
                raise ValueError("أدخل مادة وكمية على الأقل")
            return inputs

        def save(_=None) -> None:
            try:
                inputs = build_inputs()
                if (type_dd.value or "sale") == "sale" and not getattr(self, "_margin_override", False):
                    warnings = self._sale_margin_warnings(inputs, item_map)
                    if warnings:
                        self._margin_override = True
                        self.notify(warnings[0] + " — احفظ مرة أخرى للتأكيد", kind="warning")
                        return
                self._margin_override = False
                is_sale = (type_dd.value or "sale") == "sale"
                saved_id = self.ctx.invoices.create_invoice(
                    invoice_type=type_dd.value or "sale",
                    lines=inputs,
                    customer_id=int(party_dd.value) if (is_sale and party_dd.value) else None,
                    supplier_id=int(party_dd.value) if (not is_sale and party_dd.value) else None,
                    invoice_date=(invoice_date.value or "").strip(),
                    reference=reference.value,
                    notes=notes.value,
                    paid_amount=currency.parse_display_input(paid.value, settings),
                )
                draft_saved["saved"] = True
                clear_draft()
                close()
                self.notify(f"تم حفظ الفاتورة #{saved_id}", sound_kind="save")
                self.show_center()
                if is_sale:
                    # Same PHASE10 refresh the page editor triggers: a sale
                    # moves the numbers on the home screen widget.
                    refresh_home_widget(self.page, self.native_files, self.ctx.dashboard, self.ctx.settings)
                if self.on_saved:
                    self.on_saved()
            except Exception as exc:
                self.notify(str(exc), kind="error")

        def type_changed(_=None) -> None:
            self.invoice_type = type_dd.value or "sale"
            party_options()
            # Prices are invoice-type dependent; refill selected rows --
            # exactly what the page editor's type_changed does.
            for index, row in enumerate(lines_grid.rows):
                if not row.get("item"):
                    continue
                item_row = item_map.get(int(row["item"]))
                if item_row is not None:
                    lines_grid.set_cell(index, "price", default_display_price(item_row))
            update_party_insight()
            refresh_summary()

        type_dd.on_change = type_changed
        party_dd.on_change = lambda e: (update_party_insight(), refresh_summary())

        # -------------------------------------------------------------- steps
        step1_body = ft.Column(
            [
                draft_banner,
                ft.Column([ft.Text("نوع الفاتورة", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY), type_dd], spacing=6, tight=True),
                ft.Column([party_dd, party_insight], spacing=6, tight=True),
                ft.ResponsiveRow(
                    [
                        ft.Container(invoice_date, col={"xs": 12, "md": 6}),
                        ft.Container(reference, col={"xs": 12, "md": 6}),
                    ],
                    spacing=7, run_spacing=7,
                ),
                ft.Container(
                    ft.Text("بدون اختيار عميل / مورد تُحسب الفاتورة نقدية تلقائيًا", size=10, color=Colors.PRIMARY, weight=ft.FontWeight.W_600),
                    padding=ft.padding.symmetric(horizontal=10, vertical=7), bgcolor=Colors.PRIMARY_BG, border_radius=12,
                ),
            ],
            spacing=12, tight=True,
        )

        step2_body = ft.Column(
            [
                ft.Row(
                    [
                        ft.FilledButton("سطر جديد", icon=ft.Icons.ADD, on_click=add_blank),
                        ft.OutlinedButton("مسح باركود", icon=ft.Icons.QR_CODE_SCANNER, on_click=scan_line, visible=supports("barcode_camera_scan")),
                    ],
                    spacing=8, wrap=True,
                ),
                ft.Container(lines_grid, height=300, border=ft.border.all(1, Colors.BORDER), border_radius=Radius.MD),
                ft.Text("انقر مرتين على خلية المادة أو اضغط Enter للبحث والاختيار • Tab للتنقل", size=10, color=Colors.TEXT_SECONDARY),
            ],
            spacing=10, tight=True,
        )

        step3_body = ft.Column(
            [
                ft.Container(
                    ft.Column(
                        [
                            ft.Row([ft.Text("ملخص الفاتورة", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY), ft.Container(expand=True), lines_summary_text], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            ft.Row(
                                [
                                    ft.Column([ft.Text("إجمالي الفاتورة", size=12, color=Colors.TEXT_SECONDARY), total_text], spacing=2, expand=True),
                                    ft.Column([ft.Text("المتبقي", size=12, color=Colors.TEXT_SECONDARY), remaining_text], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.END, expand=True),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            paid_progress,
                        ],
                        spacing=8, tight=True,
                    ),
                    padding=12, bgcolor=Colors.BACKGROUND, border_radius=12,
                ),
                ft.Column([ft.Text("الدفعة الأولى", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY), quick_chips, paid], spacing=8, tight=True),
                notes,
            ],
            spacing=12, tight=True,
        )

        steps = [step1_body, step2_body, step3_body]
        step_titles = [
            "بيانات الفاتورة — النوع والطرف والتاريخ",
            "بنود الفاتورة — اختر المادة والكمية والسعر",
            "الدفع والمراجعة — الدفعة الأولى والملاحظات",
        ]

        title_text = ft.Text("فاتورة بيع جديدة", size=18, weight=ft.FontWeight.BOLD)
        subtitle_text = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
        icon_bubble = ft.Container(ft.Icon(ft.Icons.TRENDING_UP, color=Colors.SUCCESS, size=22), width=44, height=44, alignment=ft.alignment.center, bgcolor=Colors.SUCCESS_BG, border_radius=14)
        body_holder = ft.Column([], spacing=14, tight=True)
        footer_holder = ft.Row([], spacing=10, alignment=ft.MainAxisAlignment.END)
        dots = ft.Row([], spacing=6, alignment=ft.MainAxisAlignment.CENTER)

        def render_step() -> None:
            idx = step["idx"]
            is_sale = (type_dd.value or "sale") == "sale"
            title_text.value = f"فاتورة {'بيع' if is_sale else 'شراء'} جديدة"
            icon_bubble.content = ft.Icon(ft.Icons.TRENDING_UP if is_sale else ft.Icons.TRENDING_DOWN, color=Colors.SUCCESS if is_sale else Colors.PURPLE, size=22)
            icon_bubble.bgcolor = Colors.SUCCESS_BG if is_sale else Colors.PURPLE_BG
            subtitle_text.value = f"الخطوة {['١', '٢', '٣'][idx]} من ٣ — {step_titles[idx]}"
            body_holder.controls = [steps[idx]]
            dots.controls = [
                ft.Container(width=7, height=7, border_radius=4, bgcolor=Colors.PRIMARY if i == idx else Colors.BORDER_STRONG)
                for i in range(len(steps))
            ]
            if idx == 0:
                footer_holder.controls = [
                    ft.TextButton("إلغاء", on_click=close),
                    ft.FilledButton("التالي", icon=ft.Icons.ARROW_BACK_ROUNDED, on_click=lambda _: go(1)),
                ]
            elif idx < len(steps) - 1:
                footer_holder.controls = [
                    ft.TextButton("رجوع", icon=ft.Icons.ARROW_FORWARD_ROUNDED, on_click=lambda _: go(-1)),
                    ft.FilledButton("التالي", icon=ft.Icons.ARROW_BACK_ROUNDED, on_click=lambda _: go(1)),
                ]
            else:
                footer_holder.controls = [
                    ft.TextButton("رجوع", icon=ft.Icons.ARROW_FORWARD_ROUNDED, on_click=lambda _: go(-1)),
                    ft.FilledButton("حفظ الفاتورة", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
                ]

        def go(delta: int) -> None:
            step["idx"] = max(0, min(len(steps) - 1, step["idx"] + delta))
            render_step()
            if step["opened"]:
                self.page.update()

        sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)

        def close(_=None) -> None:
            # The grid took over the page's keyboard handler when built;
            # hand it back so the shell's shortcuts (and the list grid's
            # arrow/Enter navigation) keep working.
            persist_draft()
            self.page.close(sheet)
            self.page.on_keyboard_event = previous_key_handler

        sheet.on_dismiss = lambda e: (persist_draft(), setattr(self.page, "on_keyboard_event", previous_key_handler))

        # ----------------------------------------------------------- initial
        party_options()
        if party_id:
            try:
                party_dd.value = str(int(party_id))
            except Exception:
                pass
        prefill_lines = prefill_lines or []
        for line in prefill_lines:
            item_id = line.get("item_id")
            if not item_id:
                continue
            item_id = int(item_id)
            item_row = item_map.get(item_id)
            index = lines_grid.add_row({"qty": line.get("quantity") or 1})
            lines_grid.set_cell(index, "item", item_id)
            price_usd = line.get("unit_price")
            if price_usd:
                lines_grid.set_cell(index, "price", round(currency.to_display(float(price_usd), rate), 2))
            elif item_row is not None:
                lines_grid.set_cell(index, "price", default_display_price(item_row))
        if not prefill_lines:
            # Three blank rows, same head start the bulk-entry sheet gives.
            for _ in range(3):
                lines_grid.add_row({"qty": 1})
        update_party_insight()
        refresh_summary()

        draft = load_draft()
        if draft and draft.get("type") in (None, "", invoice_type):
            show_draft_banner(draft)

        render_step()
        sheet.content = ft.Container(
            ft.Column(
                [
                    ft.Row([ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10)], alignment=ft.MainAxisAlignment.CENTER),
                    ft.Row(
                        [icon_bubble, ft.Column([title_text, subtitle_text], spacing=1, expand=True), header_close_button(close)],
                        spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    dots,
                    ft.Divider(height=1, color=Colors.BORDER_ALT),
                    body_holder,
                    footer_holder,
                ],
                spacing=14, tight=True, scroll=ft.ScrollMode.AUTO,
            ),
            padding=ft.padding.only(left=20, right=20, top=12, bottom=24),
            bgcolor=Colors.WHITE,
            border_radius=ft.border_radius.only(top_left=28, top_right=28),
            shadow=Shadow.LG,
        )
        self.page.open(sheet)
        step["opened"] = True

    def show_table_invoice_sheet(self, invoice_type: str | None = None) -> None:
        """Comprehensive invoice entry: one wide table sheet for sale/purchase.

        Same table language as the materials bulk-entry sheet: the full grid
        toolkit (column visibility, density, sorting, multi-select with batch
        duplicate/delete, per-row menu, CSV export and live footer totals)
        plus a per-line unit picker with the page editor's smart price chain
        (per-unit override, else base price x factor). Saving reuses the
        wizard's exact rules: the same ``InvoiceLineInput`` shape and the
        same margin guard before ``create_invoice``.
        """
        invoice_type = invoice_type if invoice_type in ("sale", "purchase") else "sale"
        self.invoice_type = invoice_type
        settings = self.ctx.settings
        rate = currency.get_effective_rate(settings)
        customers = self.ctx.customers.list()
        suppliers = self.ctx.suppliers.list()
        items = self.ctx.items.list()
        item_map = {int(i["id"]): i for i in items}
        previous_key_handler = getattr(self.page, "on_keyboard_event", None)

        def _safe_update(control) -> None:
            try:
                control.update()
            except Exception:
                pass

        hidden_state = {"keys": set(filter(None, settings.get("ui.table_invoice_hidden_columns", "").split(",")))}
        density_state = {"compact": settings.get("ui.table_invoice_density", "comfortable") == "compact"}

        # ------------------------------------------------------------- header
        type_dd = SegmentedToggle(
            options=[
                SegmentOption("sale", "بيع", ft.Icons.TRENDING_UP),
                SegmentOption("purchase", "شراء", ft.Icons.TRENDING_DOWN),
            ],
            value=invoice_type,
        )
        party_dd = SearchSelect(label="العميل")
        party_insight = ft.Container(visible=False)
        invoice_date = SmartDateField(label="التاريخ", value=date.today().isoformat())
        reference = SelectAllTextField(label="المرجع")
        notes = SelectAllTextField(label="ملاحظات", multiline=True, min_lines=1, max_lines=2)

        def party_options() -> None:
            is_sale = (type_dd.value or "sale") == "sale"
            source = customers if is_sale else suppliers
            party_dd.label = "العميل" if is_sale else "المورد"
            party_dd.set_choices([(str(p["id"]), p["name"]) for p in source])
            if party_dd.value and not any(str(p["id"]) == party_dd.value for p in source):
                party_dd.value = None

        def update_party_insight() -> None:
            is_sale = (type_dd.value or "sale") == "sale"
            text = self._party_insight_text(is_sale, int(party_dd.value)) if party_dd.value else None
            if not text:
                party_insight.visible = False
                _safe_update(party_insight)
                return
            party_insight.content = ft.Row(
                [
                    ft.Icon(ft.Icons.LIGHTBULB_OUTLINE, size=15, color=Colors.PRIMARY),
                    ft.Text(text, size=11, color=Colors.PRIMARY_DARK, expand=True),
                ],
                spacing=8,
            )
            party_insight.padding = ft.padding.symmetric(horizontal=10, vertical=8)
            party_insight.bgcolor = Colors.PRIMARY_BG
            party_insight.border = ft.border.all(1, Colors.PRIMARY_BORDER)
            party_insight.border_radius = 10
            party_insight.visible = True
            _safe_update(party_insight)

        # ------------------------------------------------- lines: units/prices
        def _units_of(item_id: int) -> list[dict]:
            try:
                return self.ctx.items.units(int(item_id))
            except Exception:
                return []

        def _unit_label(unit_row: dict) -> str:
            return f"{unit_row.get('name')} × {self._qty(unit_row.get('conversion_factor'))}"

        def _unit_price_display(item_row: dict, unit_row: dict | None) -> float:
            """Same smart chain as the page editor: per-unit override wins,
            else base price x the unit's factor."""
            key = "purchase_price" if (type_dd.value or "sale") == "purchase" else "selling_price"
            if unit_row is not None:
                override = unit_row.get(key)
                if override not in (None, ""):
                    return round(currency.to_display(float(override), rate), 2)
            base = currency.to_display(float(item_row.get(key) or 0), rate)
            factor = float((unit_row or {}).get("conversion_factor") or 1)
            return round(base * factor, 2)

        def row_formatter(col, value, row):
            if col.key == "item":
                if not value:
                    return "— اختر مادة —"
                item_row = item_map.get(int(value))
                return item_row["name"] if item_row else "—"
            if col.key == "unit":
                if not value or not row.get("item"):
                    return None
                unit_row = next(
                    (u for u in _units_of(int(row["item"])) if int(u["id"]) == int(value)),
                    None,
                )
                return _unit_label(unit_row) if unit_row else None
            if col.key == "price":
                return currency.format_plain(value) if value else ""
            if col.key == "_total":
                try:
                    total = float(row.get("qty") or 0) * float(row.get("price") or 0)
                except (TypeError, ValueError):
                    total = 0.0
                return currency.format_plain(round(total, 2)) if total else ""
            if col.key == "_status":
                if not row.get("item"):
                    return ("بلا مادة", Colors.TEXT_FAINT, Colors.BACKGROUND_ALT)
                try:
                    qty = float(row.get("qty") or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                if qty <= 0:
                    return ("كمية غير صالحة", Colors.DANGER_DARKER, Colors.DANGER_BG)
                try:
                    price = float(row.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price < 0:
                    return ("سعر غير صالح", Colors.DANGER_DARKER, Colors.DANGER_BG)
                if price == 0:
                    return ("بلا سعر", Colors.ORANGE_DARK, Colors.WARNING_BG_ALT)
                return ("جاهز", Colors.SUCCESS_DARK, Colors.SUCCESS_BG)
            return None

        line_columns = [
            GridColumn("item", "المادة", editable=True, kind="picker", tooltip="نقرة على الخلية ثم Enter للبحث والاختيار داخلها"),
            GridColumn("unit", "الوحدة", width=140, editable=True, kind="picker", tooltip="وحدة البند — تغيّر معامل التحويل والسعر"),
            GridColumn("qty", "الكمية", width=90, align="end", kind="number", editable=True),
            GridColumn("price", currency.amount_field_label("السعر", settings), width=140, align="end", kind="money", editable=True),
            GridColumn("_total", "الإجمالي", width=130, align="end", tooltip="الكمية × السعر"),
            GridColumn("_status", "الحالة", width=130, align="center", sortable=False, tooltip="جاهز للحفظ / بلا مادة / كمية أو سعر غير صالح"),
        ]

        bulk_bar = ft.Container(
            visible=False,
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            border_radius=Radius.SM,
            bgcolor=Colors.PRIMARY_BG,
        )

        # A bounded viewport of its own: `expand` inside the sheet's scroll
        # body collapsed the table to zero height on Flutter (the blank
        # grey block users saw instead of the grid). Same fix as the
        # quick-entry sheet: explicit height + scrolling sheet body.
        grid_viewport = max(240, int(int(self.page.height or 800) * 0.88) - 420)
        lines_grid = DataGrid(
            columns=line_columns, rows=[], page=self.page,
            viewport_height=grid_viewport,
            row_height=36 if density_state["compact"] else 48,
            hidden_keys=set(hidden_state["keys"]),
            formatter=row_formatter,
            on_cell_commit=lambda index, key, value: commit_line_cell(index, key, value),
            picker_options=lambda col, row: (
                [
                    (int(item_row["id"]), f"{item_row['name']} · {item_row.get('barcode') or 'بلا باركود'}")
                    for item_row in items
                ]
                if col.key == "item"
                else [
                    (int(unit_row["id"]), _unit_label(unit_row))
                    for unit_row in _units_of(int(row["item"]))
                ]
                if row.get("item")
                else []
            ),
            on_sort=lambda key, direction: apply_lines_sort(key, direction),
            on_row_menu=lambda row: lines_row_menu(row),
            selectable=True,
            on_selection_change=lambda rows: render_lines_selection(rows),
            empty_text="اضغط «سطر جديد» أو «مسح باركود»",
        )

        def fill_line_defaults(index: int, item_id: int) -> None:
            """After an item is picked: choose its base unit and stamp the
            unit-aware default price (only touches blank cells)."""
            item_row = item_map.get(int(item_id))
            if item_row is None:
                return
            units = _units_of(item_id)
            unit_row = _default_invoice_unit(units)
            if unit_row is not None:
                lines_grid.set_cell(index, "unit", int(unit_row["id"]), notify=False)
            row = lines_grid.row_at(index) or {}
            if not row.get("qty"):
                lines_grid.set_cell(index, "qty", 1, notify=False)
            lines_grid.set_cell(index, "price", _unit_price_display(item_row, unit_row), notify=False)

        def commit_line_cell(index: int, key: str, value) -> None:
            if key == "item" and value:
                fill_line_defaults(index, int(value))
            elif key == "unit" and value:
                row = lines_grid.row_at(index) or {}
                item_row = item_map.get(int(row.get("item") or 0))
                if item_row is not None:
                    unit_row = next(
                        (unit for unit in _units_of(int(item_row["id"])) if int(unit["id"]) == int(value)),
                        None,
                    )
                    if unit_row is not None:
                        lines_grid.set_cell(index, "price", _unit_price_display(item_row, unit_row), notify=False)
            refresh_summary()

        def add_blank(_=None) -> None:
            lines_grid.add_row({"qty": 1})
            refresh_summary()

        def add_or_bump_row(item_id: int) -> str:
            for index, row in enumerate(lines_grid.rows):
                if row.get("item") and int(row["item"]) == item_id:
                    try:
                        qty = float(row.get("qty") or 0) + 1
                    except (TypeError, ValueError):
                        qty = 1.0
                    lines_grid.set_cell(index, "qty", qty)
                    refresh_summary()
                    return "bumped"
            index = lines_grid.add_row({"qty": 1})
            lines_grid.set_cell(index, "item", item_id)
            fill_line_defaults(index, item_id)
            refresh_summary()
            return "added"

        async def scan_line(_=None) -> None:
            if self.native_files is None:
                self.notify("مسح الباركود غير مهيأ في هذا البناء", kind="error")
                return
            try:
                code = await self.native_files.scan_barcode()
            except Exception as exc:
                self.notify(str(exc), kind="error")
                return
            if not code:
                return
            found = self.ctx.items.find_by_barcode(code)
            if not found:
                self.notify("لا توجد مادة بهذا الباركود", kind="error", sound_kind="barcode_error")
                return
            status = add_or_bump_row(int(found["id"]))
            if barcode_settings.scan_feedback_mode(settings) == "brief":
                self.notify("✔", kind="success", sound_kind="scan")
            elif status == "bumped":
                self.notify(f"✔ زيدت الكمية: {found['name']}", kind="success", sound_kind="scan")
            else:
                self.notify(f"✔ أُضيف: {found['name']}", kind="success", sound_kind="scan")

        # -------------------------------------------------- selection / menu
        def duplicate_rows(rows: list[dict]) -> None:
            for row in rows:
                if not row.get("item"):
                    continue
                index = lines_grid.add_row({
                    "qty": row.get("qty") or 1,
                    "price": row.get("price"),
                })
                lines_grid.set_cell(index, "item", int(row["item"]))
                if row.get("unit"):
                    lines_grid.set_cell(index, "unit", int(row["unit"]))
            if rows:
                refresh_summary()

        def delete_row(target: dict) -> None:
            for index, row in enumerate(lines_grid.rows):
                if row == target:
                    lines_grid.remove_row(index)
                    break
            refresh_summary()

        def lines_row_menu(row: dict) -> list:
            return [
                ("تكرار السطر", ft.Icons.CONTENT_COPY_OUTLINED, lambda r=row: duplicate_rows([dict(r)])),
                ("حذف السطر", ft.Icons.DELETE_OUTLINE, lambda r=row: delete_row(r)),
            ]

        def remove_selected_rows() -> None:
            selected = lines_grid.selected_rows()
            if not selected:
                return
            rows_now = lines_grid.rows
            doomed: list[int] = []
            for wanted in selected:
                for index, row in enumerate(rows_now):
                    if index not in doomed and row == wanted:
                        doomed.append(index)
                        break
            for index in sorted(doomed, reverse=True):
                lines_grid.remove_row(index)
            refresh_summary()

        def render_lines_selection(rows: list[dict]) -> None:
            if not rows:
                bulk_bar.visible = False
                bulk_bar.content = None
            else:
                bulk_bar.content = ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECK_BOX, size=16, color=Colors.PRIMARY),
                        ft.Text(f"تم تحديد {len(rows)} سطر", size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK),
                        ft.OutlinedButton("تكرار المحدد", icon=ft.Icons.CONTENT_COPY_OUTLINED, on_click=lambda _=None: duplicate_rows(list(rows))),
                        ft.OutlinedButton(
                            "حذف المحدد", icon=ft.Icons.DELETE_OUTLINE,
                            style=ft.ButtonStyle(color=Colors.DANGER),
                            on_click=lambda _=None: remove_selected_rows(),
                        ),
                        ft.TextButton("إلغاء التحديد", on_click=lambda _=None: lines_grid.clear_selection()),
                    ],
                    spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                bulk_bar.visible = True
            _safe_update(bulk_bar)

        def apply_lines_sort(key, direction) -> None:
            if key:
                def sort_value(row: dict):
                    value = row.get(key)
                    if key in ("qty", "price", "_total"):
                        try:
                            if key == "_total":
                                return float(row.get("qty") or 0) * float(row.get("price") or 0)
                            return float(value or 0)
                        except (TypeError, ValueError):
                            return 0.0
                    if key == "item":
                        item_row = item_map.get(int(value or 0))
                        return str((item_row or {}).get("name") or "")
                    return str(value or "")

                lines_grid.set_rows(sorted(lines_grid.rows, key=sort_value, reverse=direction == "desc"))
            refresh_summary()

        def toggle_column(key: str) -> None:
            if key in hidden_state["keys"]:
                hidden_state["keys"].discard(key)
            else:
                hidden_state["keys"].add(key)
            settings.set("ui.table_invoice_hidden_columns", ",".join(sorted(hidden_state["keys"])))
            lines_grid.set_hidden_keys(hidden_state["keys"])
            self.page.update()

        def toggle_density(_=None) -> None:
            compact = not density_state["compact"]
            density_state["compact"] = compact
            lines_grid.set_row_height(36 if compact else 48)
            density_button.icon = ft.Icons.DENSITY_SMALL if compact else ft.Icons.DENSITY_MEDIUM
            density_button.tooltip = "عرض مريح" if compact else "عرض مضغوط"
            settings.set("ui.table_invoice_density", "compact" if compact else "comfortable")
            self.page.update()

        async def export_csv(_=None) -> None:
            rows = [row for row in lines_grid.rows if row.get("item")]
            if not rows:
                self.notify("لا توجد بنود جاهزة للتصدير", kind="warning")
                return
            if self.native_files is None:
                self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                path = Path(tempfile.gettempdir()) / f"nano_table_invoice_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = _csv.writer(fh)
                    writer.writerow(["المادة", "الوحدة", "الكمية", "السعر", "الإجمالي"])
                    for row in rows:
                        item_row = item_map.get(int(row["item"])) or {}
                        unit_row = next(
                            (u for u in _units_of(int(row["item"])) if int(u["id"]) == int(row.get("unit") or 0)),
                            None,
                        )
                        try:
                            total = float(row.get("qty") or 0) * float(row.get("price") or 0)
                        except (TypeError, ValueError):
                            total = 0.0
                        writer.writerow([
                            item_row.get("name") or "",
                            _unit_label(unit_row) if unit_row else "",
                            row.get("qty") or 0,
                            row.get("price") or 0,
                            round(total, 2),
                        ])
                await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير بنود الفاتورة")
                self.notify("تم تجهيز ملف CSV للبنود الجاهزة")
            except Exception as exc:
                self.notify(str(exc), kind="error")

        # ------------------------------------------------------ payment / save
        paid = SmartAmountField(
            label=currency.amount_field_label("الدفعة الأولى", settings),
            value="0",
            on_change=lambda e: refresh_summary(),
        )
        lines_summary_text = ft.Text("لا بنود بعد", size=10, color=Colors.TEXT_SECONDARY)
        total_text = ft.Text("0", size=18, weight=ft.FontWeight.BOLD)
        remaining_text = ft.Text("0", size=16, weight=ft.FontWeight.BOLD)

        def rows_total_display() -> float:
            total = 0.0
            for row in lines_grid.rows:
                if not row.get("item"):
                    continue
                try:
                    total += float(row.get("qty") or 0) * float(row.get("price") or 0)
                except (TypeError, ValueError):
                    continue
            return total

        def refresh_summary(_=None) -> None:
            total = rows_total_display()
            count = sum(1 for row in lines_grid.rows if row.get("item"))
            qty_total = 0.0
            for row in lines_grid.rows:
                if not row.get("item"):
                    continue
                try:
                    qty_total += float(row.get("qty") or 0)
                except (TypeError, ValueError):
                    continue
            if count:
                lines_grid.set_footer({
                    "item": f"{count} بند",
                    "qty": self._qty(qty_total),
                    "_total": currency.format_plain(round(total, 2)),
                })
            else:
                lines_grid.set_footer(None)
            lines_summary_text.value = f"{count} بند" if count else "لا بنود بعد"
            # Anonymous invoices are cash by definition (same rule the wizard
            # and the accounting service enforce): no party -> paid == total.
            cash_without_party = not bool(party_dd.value)
            paid.disabled = cash_without_party
            paid.label = "مدفوع نقدًا (تلقائي)" if cash_without_party else currency.amount_field_label("الدفعة الأولى", settings)
            if cash_without_party:
                paid.value = currency.format_plain(total)
                paid_value = total
            else:
                try:
                    paid_value = float(paid.value or 0)
                except Exception:
                    paid_value = 0.0
            total_text.value = currency.format_display_value(total, settings)
            remaining_text.value = currency.format_display_value(max(0.0, total - paid_value), settings)
            remaining_text.color = Colors.SUCCESS if total - paid_value <= 1e-9 else Colors.ORANGE
            quick_chips.visible = not cash_without_party
            _safe_update(lines_summary_text)
            _safe_update(paid)
            _safe_update(total_text)
            _safe_update(remaining_text)
            _safe_update(quick_chips)

        def quick_pay(ratio: float) -> None:
            paid.value = currency.format_plain(round(rows_total_display() * ratio, 2))
            _safe_update(paid)
            refresh_summary()

        def quick_pay_chip(label: str, ratio: float, icon) -> ft.Container:
            return ft.Container(
                ft.Row([ft.Icon(icon, size=14, color=Colors.PRIMARY), ft.Text(label, size=11, weight=ft.FontWeight.W_600)], spacing=5),
                padding=ft.padding.symmetric(horizontal=10, vertical=7),
                border=ft.border.all(1, Colors.PRIMARY), border_radius=12,
                bgcolor=Colors.WHITE, ink=True,
                on_click=lambda _: quick_pay(ratio),
            )

        quick_chips = ft.Row(
            [
                quick_pay_chip("دفعة كاملة", 1.0, ft.Icons.CHECK_CIRCLE_OUTLINE),
                quick_pay_chip("النصف", 0.5, ft.Icons.PERCENT),
                quick_pay_chip("بدون دفعة", 0.0, ft.Icons.MONEY_OFF_OUTLINED),
            ],
            spacing=6, wrap=True,
        )

        def build_inputs() -> list[InvoiceLineInput]:
            inputs: list[InvoiceLineInput] = []
            for idx, row in enumerate(lines_grid.rows, start=1):
                if not row.get("item"):
                    continue
                item_id = int(row["item"])
                try:
                    qty = float(row.get("qty") or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                if qty <= 0:
                    raise ValueError(f"كمية البند {idx} غير صحيحة")
                try:
                    unit_price = (
                        currency.parse_display_input(str(row.get("price")), settings)
                        if row.get("price") not in (None, "")
                        else 0.0
                    )
                except Exception as exc:
                    raise ValueError(f"سعر البند {idx} غير صحيح") from exc
                units = _units_of(item_id)
                unit_row = None
                if row.get("unit"):
                    unit_row = next((u for u in units if int(u["id"]) == int(row["unit"])), None)
                if unit_row is None:
                    unit_row = _default_invoice_unit(units)
                inputs.append(
                    InvoiceLineInput(
                        description="",
                        item_id=item_id,
                        unit_id=int(unit_row["id"]) if unit_row else None,
                        conversion_factor=float((unit_row or {}).get("conversion_factor") or 1),
                        quantity=qty,
                        unit_price=unit_price,
                    )
                )
            if not inputs:
                raise ValueError("أدخل مادة وكمية على الأقل")
            return inputs

        def save(_=None) -> None:
            try:
                inputs = build_inputs()
                if (type_dd.value or "sale") == "sale" and not getattr(self, "_margin_override", False):
                    warnings = self._sale_margin_warnings(inputs, item_map)
                    if warnings:
                        self._margin_override = True
                        self.notify(warnings[0] + " — احفظ مرة أخرى للتأكيد", kind="warning")
                        return
                self._margin_override = False
                is_sale = (type_dd.value or "sale") == "sale"
                saved_id = self.ctx.invoices.create_invoice(
                    invoice_type=type_dd.value or "sale",
                    lines=inputs,
                    customer_id=int(party_dd.value) if (is_sale and party_dd.value) else None,
                    supplier_id=int(party_dd.value) if (not is_sale and party_dd.value) else None,
                    invoice_date=(invoice_date.value or "").strip(),
                    reference=reference.value,
                    notes=notes.value,
                    paid_amount=currency.parse_display_input(paid.value, settings),
                )
                close()
                self.notify(f"تم حفظ الفاتورة #{saved_id}", sound_kind="save")
                self.show_center()
                if is_sale:
                    # Same PHASE10 refresh the wizard/page editor trigger.
                    refresh_home_widget(self.page, self.native_files, self.ctx.dashboard, self.ctx.settings)
                if self.on_saved:
                    self.on_saved()
            except Exception as exc:
                self.notify(str(exc), kind="error")

        def type_changed(_=None) -> None:
            self.invoice_type = type_dd.value or "sale"
            party_options()
            is_sale = self.invoice_type == "sale"
            icon_bubble.content = ft.Icon(ft.Icons.TRENDING_UP if is_sale else ft.Icons.TRENDING_DOWN, color=Colors.SUCCESS if is_sale else Colors.PURPLE, size=22)
            icon_bubble.bgcolor = Colors.SUCCESS_BG if is_sale else Colors.PURPLE_BG
            # Prices are invoice-type dependent; refill selected rows with the
            # unit-aware default -- exactly what the page editor's type
            # change does.
            for index, row in enumerate(lines_grid.rows):
                if not row.get("item"):
                    continue
                item_row = item_map.get(int(row["item"]))
                if item_row is None:
                    continue
                units = _units_of(int(row["item"]))
                unit_row = next((u for u in units if int(u["id"]) == int(row.get("unit") or 0)), None)
                if unit_row is None:
                    unit_row = _default_invoice_unit(units)
                lines_grid.set_cell(index, "price", _unit_price_display(item_row, unit_row))
            update_party_insight()
            refresh_summary()

        type_dd.on_change = type_changed
        party_dd.on_change = lambda e: (update_party_insight(), refresh_summary())

        # ---------------------------------------------------------- controls
        columns_button = ft.PopupMenuButton(
            icon=ft.Icons.VIEW_COLUMN,
            tooltip="إظهار/إخفاء الأعمدة",
            items=[
                ft.PopupMenuItem(
                    text=col.label,
                    checked=col.key not in hidden_state["keys"],
                    on_click=lambda _, k=col.key: toggle_column(k),
                )
                for col in line_columns
            ],
        )
        density_button = ft.IconButton(
            icon=ft.Icons.DENSITY_SMALL if density_state["compact"] else ft.Icons.DENSITY_MEDIUM,
            tooltip="عرض مريح" if density_state["compact"] else "عرض مضغوط",
            on_click=toggle_density,
        )
        export_button = ft.IconButton(
            icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
            tooltip="تصدير البنود الجاهزة CSV",
            on_click=export_csv,
        )
        icon_bubble = ft.Container(
            ft.Icon(ft.Icons.TRENDING_UP, color=Colors.SUCCESS, size=22),
            width=44, height=44, alignment=ft.alignment.center,
            bgcolor=Colors.SUCCESS_BG, border_radius=14,
        )

        # A wide, tall sheet: the table is the point, so it gets the full
        # workbench width instead of the narrow default column.
        page_w = int(self.page.width or 1280)
        page_h = int(self.page.height or 800)
        sheet_w = max(360, page_w - 32)
        sheet_h = max(460, int(page_h * 0.88))

        sheet = ft.BottomSheet(
            content=ft.Container(),
            is_scroll_controlled=True,
            enable_drag=True,
            maintain_bottom_view_insets_padding=True,
            size_constraints=ft.BoxConstraints(min_width=sheet_w, max_width=sheet_w),
        )

        def close(_=None) -> None:
            # The grid took over the page's keyboard handler when built;
            # hand it back so the shell's shortcuts (and the list grid's
            # arrow/Enter navigation) keep working.
            self.page.close(sheet)
            self.page.on_keyboard_event = previous_key_handler

        sheet.on_dismiss = lambda e: setattr(self.page, "on_keyboard_event", previous_key_handler)

        # ----------------------------------------------------------- initial
        party_options()
        lines_grid.add_row({"qty": 1})
        update_party_insight()
        refresh_summary()

        sheet.content = ft.Container(
            ft.Column(
                [
                    ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                    ft.Row(
                        [
                            icon_bubble,
                            ft.Column(
                                [
                                    ft.Text("فاتورة شاملة (جدول)", size=17, weight=ft.FontWeight.BOLD),
                                    ft.Text("بيع أو شراء: كل البيانات والبنود في جدول واحد، ثم احفظ مباشرة", size=11, color=Colors.TEXT_SECONDARY),
                                ],
                                spacing=1, expand=True,
                            ),
                            header_close_button(close),
                        ],
                        spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.Column([ft.Text("نوع الفاتورة", size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY), type_dd], spacing=4, tight=True),
                            ft.Container(party_dd, width=240),
                            ft.Container(invoice_date, width=180),
                            ft.Container(reference, width=220),
                        ],
                        spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.Container(party_insight, expand=True),
                            ft.Container(notes, width=320),
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.FilledButton("سطر جديد", icon=ft.Icons.ADD, on_click=add_blank),
                            ft.OutlinedButton("مسح باركود", icon=ft.Icons.QR_CODE_SCANNER, on_click=scan_line, visible=supports("barcode_camera_scan")),
                            ft.Text("نقرة ثم Enter للتحرير • Tab للتنقل • Esc للإلغاء", size=10, color=Colors.TEXT_SECONDARY),
                            columns_button,
                            density_button,
                            export_button,
                        ],
                        spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bulk_bar,
                    ft.Container(lines_grid, height=grid_viewport, border=ft.border.all(1, Colors.BORDER), border_radius=Radius.MD, clip_behavior=ft.ClipBehavior.HARD_EDGE),
                    ft.Row(
                        [
                            lines_summary_text,
                            ft.Container(expand=True),
                            ft.Text("إجمالي الفاتورة", size=11, color=Colors.TEXT_SECONDARY),
                            total_text,
                            ft.Container(width=14),
                            ft.Text("المتبقي", size=11, color=Colors.TEXT_SECONDARY),
                            remaining_text,
                        ],
                        spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            quick_chips,
                            ft.Container(width=12),
                            ft.Container(paid, width=220),
                            ft.FilledButton("حفظ الفاتورة", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
                        ],
                        spacing=8, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER,
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
        self.page.open(sheet)

    def show_editor(
        self,
        invoice_id: int | None = None,
        invoice_type: str | None = None,
        *,
        prefill_lines: list[dict] | None = None,
        party_id: int | None = None,
    ) -> None:
        if invoice_id is None:
            # Creating a new invoice now goes through the 3-step bottom
            # sheet (same pattern as creating a material); the full-page
            # editor below stays for editing existing invoices.
            self.show_new_invoice_sheet(invoice_type, prefill_lines=prefill_lines, party_id=party_id)
            return
        existing = self.ctx.invoices.get_invoice(invoice_id) if invoice_id else None
        if invoice_id and not existing:
            self.notify("الفاتورة غير موجودة")
            return

        self.editing_id = invoice_id
        self.invoice_type = (existing["type"] if existing else invoice_type) or "sale"
        if self.on_title_change:
            kind_label = "بيع" if self.invoice_type == "sale" else "شراء"
            self.on_title_change(
                f"تعديل فاتورة #{invoice_id}" if invoice_id else f"فاتورة {kind_label}",
                "إدارة بيانات الفاتورة والبنود والدفع" if invoice_id else f"إنشاء فاتورة {kind_label} جديدة — النقدي افتراضيًا",
            )
        customers = self.ctx.customers.list()
        suppliers = self.ctx.suppliers.list()
        items = self.ctx.items.list()
        item_map = {int(i["id"]): i for i in items}
        best_sellers = [i for i in self.ctx.items.pos_catalog(limit_best_sellers=10) if i.get("sold_qty")]

        type_dd = SegmentedToggle(
            options=[
                SegmentOption("sale", "بيع", ft.Icons.TRENDING_UP),
                SegmentOption("purchase", "شراء", ft.Icons.TRENDING_DOWN),
            ],
            value=self.invoice_type,
        )
        party_dd = SearchSelect(label="العميل")
        party_insight = ft.Container(visible=False)
        suggestions_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO)
        suggestions_mode = {"key": "best"}

        def mode_chip(text: str, key: str) -> ft.Container:
            active = suggestions_mode["key"] == key
            return ft.Container(
                ft.Text(text, size=10, color=Colors.WHITE if active else Colors.TEXT_MUTED, weight=ft.FontWeight.W_600 if active else ft.FontWeight.W_500),
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                bgcolor=Colors.PRIMARY if active else Colors.WHITE,
                border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
                border_radius=12,
                ink=True,
                on_click=lambda _, k=key: set_suggestions_mode(k),
            )

        suggestions_mode_row = ft.Row(spacing=6)
        suggestions_column = ft.Column([suggestions_mode_row, suggestions_row], spacing=6, visible=False)
        invoice_date = SmartDateField(label="التاريخ", value=str(existing["invoice_date"] if existing else date.today().isoformat()))
        reference = SelectAllTextField(label="المرجع", value=(existing.get("reference") or "") if existing else "")
        notes = SelectAllTextField(label="ملاحظات", value=(existing.get("notes") or "") if existing else "", multiline=True, min_lines=1, max_lines=3)
        paid = SelectAllTextField(
            label=currency.amount_field_label("الدفعة الأولى", self.ctx.settings),
            value=currency.to_input_text(existing["initial_paid_amount"] if existing else 0, self.ctx.settings),
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        lines_column = ft.Column(spacing=8)
        total_text = ft.Text("0.00", size=20, weight=ft.FontWeight.BOLD)
        remaining_text = ft.Text("0.00", size=18, weight=ft.FontWeight.BOLD)
        paid_progress = ft.ProgressBar(value=0, color=Colors.SUCCESS, bgcolor=Colors.BACKGROUND_ALT, height=6, border_radius=6)

        def party_options() -> None:
            is_sale = type_dd.value == "sale"
            source = customers if is_sale else suppliers
            party_dd.label = "العميل" if is_sale else "المورد"
            party_dd.set_choices([(str(p["id"]), p["name"]) for p in source])
            if existing and existing["type"] == type_dd.value:
                pid = existing.get("customer_id") if is_sale else existing.get("supplier_id")
                party_dd.value = str(pid) if pid else None
            elif party_dd.value and not any(str(p["id"]) == party_dd.value for p in source):
                party_dd.value = None

        def party_insight_text(is_sale: bool, party_id: int) -> str | None:
            return self._party_insight_text(is_sale, party_id)

        def _safe_update(control) -> None:
            # A control may not be attached to the page yet the first time
            # these run (e.g. the very first render before self.page.update()
            # has ever executed) -- same guard SegmentedToggle uses.
            try:
                control.update()
            except Exception:
                pass

        def update_party_insight() -> None:
            is_sale = type_dd.value == "sale"
            if not party_dd.value:
                party_insight.visible = False
                _safe_update(party_insight)
                return
            text = party_insight_text(is_sale, int(party_dd.value))
            if not text:
                party_insight.visible = False
                _safe_update(party_insight)
                return
            party_insight.content = ft.Row(
                [
                    ft.Icon(ft.Icons.LIGHTBULB_OUTLINE, size=15, color=Colors.PRIMARY),
                    ft.Text(text, size=11, color=Colors.PRIMARY_DARK, expand=True),
                ],
                spacing=8,
            )
            party_insight.padding = ft.padding.symmetric(horizontal=10, vertical=8)
            party_insight.bgcolor = Colors.PRIMARY_BG
            party_insight.border = ft.border.all(1, Colors.PRIMARY_BORDER)
            party_insight.border_radius = 10
            party_insight.visible = True
            _safe_update(party_insight)

        def suggestion_chip(item_row: dict, sub_label: str) -> ft.Container:
            item_id = int(item_row["id"])
            return ft.Container(
                ft.Column(
                    [
                        ft.Text(item_row["name"], size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_PRIMARY, max_lines=1),
                        ft.Text(sub_label, size=9, color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=1, tight=True,
                ),
                padding=ft.padding.symmetric(horizontal=10, vertical=7),
                bgcolor=Colors.WHITE,
                border=ft.border.all(1, Colors.BORDER),
                border_radius=12,
                ink=True,
                on_click=lambda _, iid=item_id: add_or_bump_item(iid),
                width=130,
            )

        def refresh_suggestions(_=None) -> None:
            is_sale = type_dd.value == "sale"
            source: list[dict] = []
            if suggestions_mode["key"] == "history" and party_dd.value:
                source = self.ctx.items.purchased_by_party(type_dd.value or "sale", int(party_dd.value), limit=10)
            if not source:
                suggestions_mode["key"] = "best"
                source = best_sellers
            chips = []
            for row in source[:10]:
                if suggestions_mode["key"] == "history":
                    sub = f"اشتُري {int(row.get('times_bought') or 1)} مرة"
                else:
                    sub = "الأكثر مبيعًا" if is_sale else "متوفر"
                chips.append(suggestion_chip(row, sub))
            suggestions_row.controls = chips
            mode_row_controls = [mode_chip("الأكثر مبيعًا", "best")]
            if party_dd.value:
                mode_row_controls.append(mode_chip("مشترياته السابقة", "history"))
            suggestions_mode_row.controls = mode_row_controls
            suggestions_column.visible = bool(chips)
            _safe_update(suggestions_column)

        def set_suggestions_mode(key: str) -> None:
            suggestions_mode["key"] = key
            refresh_suggestions()

        def parse_number(control, label: str, allow_zero: bool = True) -> float:
            try:
                value = float(control.value or 0)
            except Exception as exc:
                raise ValueError(f"{label} غير صحيح") from exc
            if value < 0 or (not allow_zero and value <= 0):
                raise ValueError(f"{label} غير صحيح")
            return value

        def parse_money(control, label: str, allow_zero: bool = True) -> float:
            """Like ``parse_number`` but the field holds a displayed-currency figure -- convert to USD for storage."""
            try:
                display_value = float((control.value or "0").replace(",", ""))
            except Exception as exc:
                raise ValueError(f"{label} غير صحيح") from exc
            if display_value < 0 or (not allow_zero and display_value <= 0):
                raise ValueError(f"{label} غير صحيح")
            return currency.parse_display_input(control.value, self.ctx.settings)

        def recalc(_=None) -> None:
            total = 0.0
            for state in self.lines:
                try:
                    qty_value = float(state["qty"].value or 0)
                    price_value = float(state["price"].value or 0)
                    line_total = qty_value * price_value
                except Exception:
                    line_total = 0.0
                state["total"].value = currency.format_plain(line_total)
                total += line_total

            # Anonymous invoices are cash by definition: if no customer/supplier
            # is selected, Nano settles the invoice in full automatically.
            cash_without_party = not bool(party_dd.value)
            paid.disabled = cash_without_party
            paid.label = "مدفوع نقدًا (تلقائي)" if cash_without_party else currency.amount_field_label("الدفعة الأولى", self.ctx.settings)
            if cash_without_party:
                paid.value = currency.format_plain(total)
                paid_value = total
            else:
                try:
                    paid_value = float(paid.value or 0)
                except Exception:
                    paid_value = 0.0
            total_text.value = currency.format_display_value(total, self.ctx.settings)
            remaining_text.value = currency.format_display_value(max(0, total - paid_value), self.ctx.settings)
            remaining_text.color = Colors.SUCCESS if total - paid_value <= 1e-9 else Colors.ORANGE
            paid_progress.value = max(0.0, min(1.0, (paid_value / total))) if total > 1e-9 else 1.0
            self.page.update()

        def _derived_base_price(state: dict) -> tuple[float | None, str | None]:
            """Per-base-unit price (USD) derived from a sibling sub-unit.

            When the item itself carries no purchase/selling price but one of
            its sub-units has an explicit per-unit override (e.g. box =
            15000 with factor 1.5), the base-unit price is that override
            divided by its factor (15000 / 1.5 = 10000 per meter). Returns
            (price, unit name) or (None, None) when nothing can be derived.
            """
            key = "purchase_price" if type_dd.value == "purchase" else "selling_price"
            for row in (state.get("units") or {}).values():
                row = row or {}
                raw = row.get(key)
                if raw in (None, ""):
                    continue
                try:
                    price = float(raw)
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                try:
                    factor = float(row.get("conversion_factor") or 1)
                except (TypeError, ValueError):
                    continue
                if factor <= 0:
                    continue
                return price / factor, str(row.get("name") or "")
            return None, None

        def _fill_line_price(state: dict) -> None:
            """Smart price chain for one invoice line (display currency).

            1. explicit per-unit override of the chosen unit;
            2. item base price x the chosen unit's factor;
            3. derived base price (see _derived_base_price) x factor, with
               state["derived_from"] recording the source unit for the hint;
            4. zeroes when nothing is known (caller may warn, never blocks).
            """
            uid = int(state["unit"].value) if state["unit"].value else None
            urow = state.get("units", {}).get(uid) if uid else None
            key = "purchase_price" if type_dd.value == "purchase" else "selling_price"
            override = (urow or {}).get(key)
            if override not in (None, ""):
                state["price"].value = currency.format_plain(currency.to_display(float(override), currency.get_effective_rate(self.ctx.settings)))
                state["derived_from"] = None
                return
            try:
                base = float(state.get("base_price") or 0)
            except (TypeError, ValueError):
                base = 0.0
            factor = float(state.get("factor") or 1)
            if base > 0:
                state["price"].value = currency.format_plain(base * factor)
                state["derived_from"] = None
                return
            derived, dname = _derived_base_price(state)
            if derived is not None:
                state["price"].value = currency.format_plain(currency.to_display(derived * factor, currency.get_effective_rate(self.ctx.settings)))
                state["derived_from"] = dname
            else:
                state["price"].value = currency.format_plain(base * factor)
                state["derived_from"] = None

        def update_line_units(state: dict, selected_unit_id: int | None = None) -> None:
            unit_select = state["unit"]
            unit_select.options = []
            unit_select.value = None
            state["factor"] = 1.0
            if not state["item"].value:
                _safe_update(unit_select)
                return
            item_id_value = int(state["item"].value)
            units = self.ctx.items.units(item_id_value)
            state["units"] = {int(u["id"]): u for u in units}
            # قائمة منسدلة حقيقية (Dropdown) بدل الحقل البحثي: خيار واحد لكل وحدة.
            unit_select.options = [
                ft.dropdown.Option(key=str(u["id"]), text=f"{u['name']} × {self._qty(u['conversion_factor'])}")
                for u in units
            ]
            chosen = selected_unit_id if selected_unit_id in state["units"] else None
            if chosen is None and units:
                default_unit = _default_invoice_unit(units)
                chosen = int(default_unit["id"]) if default_unit else None
            if chosen is not None:
                unit_select.value = str(chosen)
                state["factor"] = float(state["units"][chosen]["conversion_factor"])
            _safe_update(unit_select)

        def item_changed(state: dict) -> None:
            if state["item"].value:
                item_row = item_map[int(state["item"].value)]
                state["description"].value = item_row["name"]
                base_price_usd = float(item_row["selling_price"] if type_dd.value == "sale" else item_row["purchase_price"])
                state["base_price"] = currency.to_display(base_price_usd, currency.get_effective_rate(self.ctx.settings))
                update_line_units(state)
                _fill_line_price(state)
            recalc()

        def unit_changed(state: dict) -> None:
            if state["unit"].value:
                uid = int(state["unit"].value)
                unit_row = state.get("units", {}).get(uid)
                state["factor"] = float(unit_row["conversion_factor"]) if unit_row else 1.0
                if state.get("base_price") is not None:
                    _fill_line_price(state)
            recalc()

        def remove_line(state: dict) -> None:
            if len(self.lines) <= 1:
                self.notify("يجب أن تحتوي الفاتورة على بند واحد على الأقل")
                return
            self.lines.remove(state)
            lines_column.controls.remove(state["card"])
            recalc()

        def refresh_stock_badge(state: dict) -> None:
            badge = state.get("stock_badge")
            if badge is None:
                return
            item_id_value = state["item"].value
            show = False
            if type_dd.value == "sale" and item_id_value:
                item_row = item_map.get(int(item_id_value))
                if item_row and item_row.get("item_type") == "مخزون":
                    stock_qty = float(item_row.get("quantity") or 0)
                    if stock_qty <= LOW_STOCK_THRESHOLD:
                        label = "نفدت الكمية في المخزون" if stock_qty <= 0 else f"متبقي {self._qty(stock_qty)} فقط في المخزون"
                        badge.content = ft.Row(
                            [
                                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=13, color=Colors.WARNING_DARKER),
                                ft.Text(label, size=10, color=Colors.WARNING_DARKER),
                            ],
                            spacing=5, tight=True,
                        )
                        badge.padding = ft.padding.symmetric(horizontal=8, vertical=4)
                        badge.bgcolor = Colors.WARNING_BG
                        badge.border_radius = 8
                        show = True
            badge.visible = show
            _safe_update(badge)

        def refresh_price_badge(state: dict) -> None:
            # بصمة السعر -- flags a purchase line whose price per base unit
            # is well off the item's recent history (same supplier first,
            # falling back to all suppliers). Purchase-only: sale prices are
            # the merchant's own choice, not something to second-guess here.
            badge = state.get("price_badge")
            if badge is None:
                return
            show = False
            if type_dd.value == "purchase" and state["item"].value:
                try:
                    price_usd = currency.parse_display_input(state["price"].value, self.ctx.settings)
                except Exception:
                    price_usd = None
                factor = float(state.get("factor") or 1)
                if price_usd is not None and price_usd >= 0 and factor > 0:
                    result = check_purchase_price(
                        self.ctx.db,
                        item_id=int(state["item"].value),
                        unit_price=price_usd,
                        conversion_factor=factor,
                        supplier_id=int(party_dd.value) if party_dd.value else None,
                        exclude_invoice_id=self.editing_id,
                    )
                    if result.get("flag"):
                        badge.content = ft.Row(
                            [
                                ft.Icon(ft.Icons.PRICE_CHANGE_OUTLINED, size=13, color=Colors.WARNING_DARKER),
                                ft.Text(result["message"], size=10, color=Colors.WARNING_DARKER, expand=True),
                            ],
                            spacing=5, tight=True,
                        )
                        badge.padding = ft.padding.symmetric(horizontal=8, vertical=4)
                        badge.bgcolor = Colors.WARNING_BG
                        badge.border_radius = 8
                        show = True
            if not show and state.get("derived_from"):
                # Smart-price hint: the line price was derived from a sibling
                # sub-unit's override (e.g. per-meter price computed from the
                # box price), not typed by the user -- say so explicitly.
                try:
                    _pv = currency.parse_display_input(state["price"].value, self.ctx.settings)
                except Exception:
                    _pv = None
                if _pv:
                    badge.content = ft.Row(
                        [
                            ft.Icon(ft.Icons.INFO_OUTLINE, size=13, color=Colors.PRIMARY),
                            ft.Text(f"سعر مشتق من {state['derived_from']}", size=10, color=Colors.PRIMARY, expand=True),
                        ],
                        spacing=5, tight=True,
                    )
                    badge.padding = ft.padding.symmetric(horizontal=8, vertical=4)
                    badge.bgcolor = Colors.PRIMARY_BG
                    badge.border_radius = 8
                    show = True
            badge.visible = show
            _safe_update(badge)

        def add_line(initial: dict | None = None) -> None:
            initial = initial or {}
            initial_item = item_map.get(int(initial["item_id"])) if initial.get("item_id") else None
            state: dict = {
                "factor": float(initial.get("conversion_factor") or 1),
                "units": {},
                "derived_from": None,
                "base_price": (
                    currency.to_display(
                        float(initial_item["selling_price"] if type_dd.value == "sale" else initial_item["purchase_price"]),
                        currency.get_effective_rate(self.ctx.settings),
                    )
                    if initial_item else None
                ),
            }
            item_dd = SearchSelect(
                label="المادة / الخدمة",
                choices=[(str(i["id"]), i["name"]) for i in items],
                value=str(initial["item_id"]) if initial.get("item_id") else None,
            )
            description = SelectAllTextField(label="البيان", value=initial.get("description") or "")
            # قائمة منسدلة حقيقية بدل الحقل البحثي -- نقرة واحدة تُظهر كل الوحدات،
            # بنفس نمط القوائم المنسدلة في شاشات الأصناف والتقارير.
            unit_dd = ft.Dropdown(
                label="الوحدة",
                hint_text="اختر الوحدة",
                options=[],
                filled=True,
                bgcolor=Colors.BACKGROUND_ALT,
                border_radius=Radius.MD,
                border_color=Colors.BORDER,
            )
            qty = SelectAllTextField(label="الكمية", value=str(initial.get("quantity") or 1), keyboard_type=ft.KeyboardType.NUMBER)
            price = SmartAmountField(
                label=currency.amount_field_label("السعر", self.ctx.settings),
                value=currency.to_input_text(initial.get("unit_price") or 0, self.ctx.settings),
            )
            line_total = ft.Text("0.00", weight=ft.FontWeight.BOLD)
            stock_badge = ft.Container(visible=False)
            price_badge = ft.Container(visible=False)
            state.update({"item": item_dd, "description": description, "unit": unit_dd, "qty": qty, "price": price, "total": line_total, "stock_badge": stock_badge, "price_badge": price_badge})

            def bump_qty(delta: float) -> None:
                try:
                    current = float(qty.value or 0)
                except Exception:
                    current = 0.0
                new_value = max(0.0, current + delta)
                qty.value = str(int(new_value)) if new_value == int(new_value) else self._qty(new_value)
                qty.update()
                recalc()
                refresh_stock_badge(state)

            item_dd.on_change = lambda e: (item_changed(state), refresh_stock_badge(state), refresh_price_badge(state))
            unit_dd.on_change = lambda e: (unit_changed(state), refresh_price_badge(state))
            qty.on_change = lambda e: (recalc(), refresh_stock_badge(state))
            price.on_change = lambda e: (recalc(), refresh_price_badge(state))
            card = ft.Container(
                content=ft.Column(
                    [
                        ft.ResponsiveRow(
                            [
                                ft.Container(item_dd, col={"xs": 12, "md": 4}),
                                ft.Container(description, col={"xs": 12, "md": 4}),
                                ft.Container(unit_dd, col={"xs": 12, "md": 4}),
                            ]
                        ),
                        ft.ResponsiveRow(
                            [
                                ft.Container(
                                    ft.Row(
                                        [
                                            stepper_icon_button(ft.Icons.REMOVE_CIRCLE_OUTLINE, lambda e: bump_qty(-1), tooltip="إنقاص"),
                                            ft.Container(qty, expand=True),
                                            stepper_icon_button(ft.Icons.ADD_CIRCLE_OUTLINE, lambda e: bump_qty(1), tooltip="زيادة"),
                                        ],
                                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    col={"xs": 12, "md": 3},
                                ),
                                ft.Container(price, col={"xs": 5, "md": 3}),
                                ft.Container(
                                    ft.Column([ft.Text("الإجمالي", size=11, color=Colors.TEXT_SECONDARY), line_total], spacing=2),
                                    col={"xs": 8, "md": 3},
                                    padding=8,
                                ),
                                ft.Container(
                                    ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="حذف البند", on_click=lambda e: remove_line(state)),
                                    col={"xs": 4, "md": 3},
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        stock_badge,
                        price_badge,
                    ],
                    spacing=4,
                ),
                padding=10,
                border=ft.border.all(1, Colors.BORDER_ALT),
                border_radius=12,
                bgcolor=Colors.WHITE,
                shadow=Shadow.SM,
            )
            state["card"] = card
            self.lines.append(state)
            lines_column.controls.append(card)
            if initial.get("item_id"):
                update_line_units(state, int(initial["unit_id"]) if initial.get("unit_id") else None)
                # `price` above was seeded from initial.get("unit_price"), which
                # callers that only know the item (e.g. barcode scan-to-add)
                # don't have -- compute it the same way item_changed() would,
                # so a scanned line shows a real price and not "0.00".
                if not initial.get("unit_price") and state.get("base_price") is not None:
                    _fill_line_price(state)
            refresh_stock_badge(state)
            refresh_price_badge(state)
            recalc()

        def add_or_bump_item(item_id: int) -> str:
            # If this item already has a line on the invoice, bump its
            # quantity by one instead of adding a duplicate row -- matches
            # how a cashier expects repeated scans/taps of the same product
            # to behave at a real point of sale. Returns "bumped"/"added" so
            # callers that want scan feedback (see scan_to_add_line) can
            # tell the two apart; the manual item-picker call site below
            # ignores the return value and stays silent, same as before.
            existing_state = next((s for s in self.lines if s["item"].value == str(item_id)), None)
            if existing_state is not None:
                try:
                    existing_state["qty"].value = str(float(existing_state["qty"].value or 0) + 1)
                except Exception:
                    existing_state["qty"].value = "1"
                existing_state["qty"].update()
                recalc()
                return "bumped"
            add_line({"item_id": item_id, "quantity": 1})
            self.page.update()
            return "added"

        async def scan_to_add_line(_):
            if self.native_files is None:
                self.notify("مسح الباركود غير مهيأ في هذا البناء", kind="error")
                return
            try:
                code = await self.native_files.scan_barcode()
            except Exception as exc:
                self.notify(str(exc), kind="error")
                return
            if not code:
                return
            found = self.ctx.items.find_by_barcode(code)
            if not found:
                self.notify("لا توجد مادة بهذا الباركود", kind="error", sound_kind="barcode_error")
                return
            # Same audible/visual scan feedback as the POS screen's barcode
            # path (core/sound.py's dedicated "scan" tone, gated by the
            # same admin "الباركود" -> "طريقة إشعار المسح" setting) -- this
            # call site used to add the line completely silently, so a
            # successful scan into an invoice looked identical to a failed
            # one that just didn't show anything yet.
            status = add_or_bump_item(int(found["id"]))
            if barcode_settings.scan_feedback_mode(self.ctx.settings) == "brief":
                self.notify("✔", kind="success", sound_kind="scan")
            elif status == "bumped":
                self.notify(f"✔ زيدت الكمية: {found['name']}", kind="success", sound_kind="scan")
            else:
                self.notify(f"✔ أُضيف: {found['name']}", kind="success", sound_kind="scan")

        self.lines = []
        party_options()
        if existing:
            for line in existing["lines"]:
                add_line(line)
        elif prefill_lines:
            for line in prefill_lines:
                add_line(line)
            if not prefill_lines:
                add_line()
        else:
            add_line()
        # Optional party preselect (customer for sale / supplier for purchase)
        if party_id and not existing:
            try:
                party_dd.value = str(int(party_id))
                party_dd.update()
            except Exception:
                pass

        def type_changed(_):
            self.invoice_type = type_dd.value or "sale"
            party_options()
            # Prices are invoice-type dependent; refresh selected rows.
            for state in self.lines:
                if state["item"].value:
                    item_row = item_map[int(state["item"].value)]
                    base_price_usd = float(item_row["selling_price"] if type_dd.value == "sale" else item_row["purchase_price"])
                    state["base_price"] = currency.to_display(base_price_usd, currency.get_effective_rate(self.ctx.settings))
                    _fill_line_price(state)
                refresh_stock_badge(state)
                refresh_price_badge(state)
            update_party_insight()
            refresh_suggestions()
            recalc()

        def party_changed(_):
            # Supplier determines the price-history scope for purchase lines.
            for state in self.lines:
                refresh_price_badge(state)
            update_party_insight()
            refresh_suggestions()
            recalc()

        type_dd.on_change = type_changed
        party_dd.on_change = party_changed
        paid.on_change = recalc

        def save(_):
            try:
                inputs: list[InvoiceLineInput] = []
                for idx, state in enumerate(self.lines, start=1):
                    item_id_value = int(state["item"].value) if state["item"].value else None
                    unit_id_value = int(state["unit"].value) if state["unit"].value else None
                    inputs.append(
                        InvoiceLineInput(
                            description=(state["description"].value or "").strip(),
                            item_id=item_id_value,
                            unit_id=unit_id_value,
                            conversion_factor=float(state.get("factor") or 1),
                            quantity=parse_number(state["qty"], f"كمية البند {idx}", allow_zero=False),
                            unit_price=parse_money(state["price"], f"سعر البند {idx}"),
                        )
                    )
                # Margin guard for sale invoices (stored USD vs average cost)
                if (type_dd.value or "sale") == "sale" and not getattr(self, "_margin_override", False):
                    warnings = self._sale_margin_warnings(inputs, item_map)
                    if warnings:
                        self._margin_override = True
                        self.notify(
                            warnings[0] + " — احفظ مرة أخرى للتأكيد",
                            kind="warning",
                        )
                        return
                self._margin_override = False
                paid_value = parse_money(paid, "المبلغ المدفوع")
                common = dict(
                    invoice_type=type_dd.value or "sale",
                    lines=inputs,
                    customer_id=int(party_dd.value) if (type_dd.value == "sale" and party_dd.value) else None,
                    supplier_id=int(party_dd.value) if (type_dd.value == "purchase" and party_dd.value) else None,
                    invoice_date=(invoice_date.value or "").strip(),
                    reference=reference.value,
                    notes=notes.value,
                    paid_amount=paid_value,
                )
                if self.editing_id:
                    self.ctx.invoices.update_invoice(self.editing_id, **common)
                    saved_id = self.editing_id
                    message = f"تم تعديل الفاتورة #{saved_id} وإعادة احتساب المخزون والأرصدة"
                else:
                    saved_id = self.ctx.invoices.create_invoice(**common)
                    message = f"تم حفظ الفاتورة #{saved_id}"
                self.notify(message, sound_kind="save")
                self.show_center()
                if type_dd.value == "sale":
                    # PHASE10: sales move the two numbers on the home screen
                    # widget (today's total, cash) -- refresh it right away
                    # instead of waiting for the next periodic pass.
                    refresh_home_widget(self.page, self.native_files, self.ctx.dashboard, self.ctx.settings)
                if self.on_saved:
                    self.on_saved()
            except Exception as exc:
                self.notify(str(exc), kind="error")

        def open_bulk_lines_sheet(_=None) -> None:
            """Table-style quick entry for invoice lines.

            Reuses the editable DataGrid (same keys as every other table)
            with an inline searchable picker, so an item is never guessed
            by name. Chosen rows are applied through the
            exact ``add_line`` path the manual editor uses, so units,
            prices and the stock/price badges stay consistent.
            """
            previous_key_handler = getattr(self.page, "on_keyboard_event", None)
            rate = currency.get_effective_rate(self.ctx.settings)
            price_key = "selling_price" if (type_dd.value or self.invoice_type) == "sale" else "purchase_price"
            def default_display_price(item_row: dict) -> float:
                price_usd = float(item_row.get(price_key) or 0)
                return round(currency.to_display(price_usd, rate), 2)

            def row_formatter(col, value, row):
                if col.key == "item":
                    if not value:
                        return "— اختر مادة —"
                    item_row = item_map.get(int(value))
                    return item_row["name"] if item_row else "—"
                if col.key == "price":
                    return currency.format_plain(value) if value else ""
                return None

            bulk_grid = DataGrid(
                columns=[
                    GridColumn("item", "المادة", editable=True, kind="picker"),
                    GridColumn("qty", "الكمية", editable=True, kind="number", width=110, align="end"),
                    GridColumn("price", "السعر", editable=True, kind="money", width=150, align="end"),
                ],
                rows=[], page=self.page,
                picker_options=lambda _col, _row: [
                    (int(item_row["id"]), f"{item_row['name']} · {item_row.get('barcode') or 'بلا باركود'}")
                    for item_row in items
                ],
                on_cell_commit=lambda index, key, value: commit_bulk_cell(index, key, value),
                formatter=row_formatter,
                empty_text="اضغط «سطر جديد» لإضافة بنود",
            )

            def refresh_footer() -> None:
                """Live summary under the grid: picked rows + running total."""
                count = 0
                total = 0.0
                for row in bulk_grid.rows:
                    if not row.get("item"):
                        continue
                    count += 1
                    total += float(row.get("qty") or 0) * float(row.get("price") or 0)
                if not count:
                    bulk_grid.set_footer(None)
                    return
                bulk_grid.set_footer({
                    "item": f"{count} بند",
                    "price": currency.format_plain(round(total, 2)),
                })

            def commit_bulk_cell(index: int, key: str, value) -> None:
                if key == "item" and value:
                    row = bulk_grid.row_at(index) or {}
                    item_row = item_map.get(int(value))
                    if item_row is not None and not row.get("price"):
                        bulk_grid.set_cell(index, "price", default_display_price(item_row), notify=False)
                refresh_footer()

            for _ in range(3):
                bulk_grid.add_row({"qty": 1})
            refresh_footer()

            def add_blank(_=None) -> None:
                bulk_grid.add_row({"qty": 1})

            def apply_lines(_=None) -> None:
                added = 0
                for row in bulk_grid.rows:
                    item_id = row.get("item")
                    if not item_id:
                        continue
                    try:
                        qty = float(row.get("qty") or 0)
                    except (TypeError, ValueError):
                        qty = 0.0
                    if qty <= 0:
                        continue
                    display_price = row.get("price") or 0
                    try:
                        unit_price_usd = currency.parse_display_input(str(display_price), self.ctx.settings) if display_price else 0.0
                    except Exception:
                        unit_price_usd = 0.0
                    add_line({"item_id": int(item_id), "quantity": qty, "unit_price": unit_price_usd})
                    added += 1
                if not added:
                    self.notify("أدخل مادة وكمية على الأقل", kind="error")
                    return
                close()
                self.notify(f"أُضيف {added} بند", kind="success")
                # add_line() appended cards to the editor behind the sheet;
                # repaint once the dismiss animation has been requested.
                try:
                    self.page.update()
                except Exception:
                    pass

            sheet = ft.BottomSheet(content=ft.Container(), is_scroll_controlled=True, enable_drag=True, maintain_bottom_view_insets_padding=True)
            sheet.on_dismiss = lambda e: setattr(self.page, "on_keyboard_event", previous_key_handler)

            def close(_=None) -> None:
                self.page.close(sheet)
                # The grid took over the page's keyboard handler when built;
                # hand it back so the app's shortcuts keep working.
                self.page.on_keyboard_event = previous_key_handler

            sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Row(
                            [
                                ft.Container(ft.Icon(ft.Icons.VIEW_LIST_OUTLINED, color=Colors.PRIMARY, size=20), width=40, height=40, alignment=ft.alignment.center, bgcolor=Colors.PRIMARY_BG, border_radius=12),
                                ft.Column(
                                    [
                                        ft.Text("إدخال سريع (جدول)", size=17, weight=ft.FontWeight.BOLD),
                                        ft.Text("اختر المادة لكل سطر ثم أدخل الكمية والسعر", size=11, color=Colors.TEXT_SECONDARY),
                                    ],
                                    spacing=1, expand=True,
                                ),
                                header_close_button(close),
                            ],
                            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Row(
                            [
                                ft.FilledButton("سطر جديد", icon=ft.Icons.ADD, on_click=add_blank),
                                ft.Text("انقر مرتين على خلية المادة أو اضغط Enter للبحث والاختيار • Tab للتنقل", size=10, color=Colors.TEXT_SECONDARY, expand=True),
                            ],
                            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Container(bulk_grid, height=260, border=ft.border.all(1, Colors.BORDER), border_radius=Radius.MD),
                        ft.Row(
                            [
                                ft.Container(expand=True),
                                ft.FilledButton("إضافة للفاتورة", icon=ft.Icons.PLAYLIST_ADD, on_click=apply_lines),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=12, tight=True, scroll=ft.ScrollMode.AUTO,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=ft.padding.only(left=18, right=18, top=12, bottom=20),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=28, top_right=28),
                shadow=Shadow.LG,
            )
            self.page.open(sheet)

        # Body scrolls; totals + save stay pinned in a sticky footer below it
        # so they're always visible even on a long line-item list, instead
        # of disappearing off the bottom of one long scrollable column.
        # Sticky top: back button + buy/sell toggle stay reachable no matter
        # how far the body is scrolled, instead of scrolling away with the
        # line items below them.
        sticky_top = ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.OutlinedButton("رجوع للفواتير", icon=ft.Icons.ARROW_FORWARD, on_click=lambda _: self.show_center()),
                            ft.Container(
                                ft.Text("نقدي تلقائيًا عند عدم اختيار عميل / مورد", size=10, color=Colors.PRIMARY, weight=ft.FontWeight.W_600),
                                padding=ft.padding.symmetric(horizontal=10, vertical=7), bgcolor=Colors.PRIMARY_BG, border_radius=14,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True,
                    ),
                    type_dd,
                ],
                spacing=10,
            ),
            padding=ft.padding.only(left=16, right=16, top=12, bottom=8),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
        )

        scroll_body = ft.Column(
            [
                ft.ResponsiveRow(
                    [
                        ft.Container(
                            ft.Column([party_dd, party_insight], spacing=6),
                            col={"xs": 12, "md": 5},
                        ),
                        ft.Container(invoice_date, col={"xs": 6, "md": 2}),
                        ft.Container(reference, col={"xs": 6, "md": 3}),
                    ]
                ),
                ft.Row(
                    [
                        ft.Text("بنود الفاتورة", size=18, weight=ft.FontWeight.BOLD, expand=True),
                        ft.OutlinedButton("مسح باركود", icon=ft.Icons.QR_CODE_SCANNER, on_click=scan_to_add_line, visible=supports("barcode_camera_scan")),
                        ft.OutlinedButton("إدخال سريع (جدول)", icon=ft.Icons.VIEW_LIST_OUTLINED, on_click=open_bulk_lines_sheet),
                        ft.OutlinedButton("إضافة بند", icon=ft.Icons.ADD, on_click=lambda _: add_line()),
                    ]
                ),
                suggestions_column,
                lines_column,
                paid,
                notes,
                ft.Text(
                    "عند تعديل أو حذف فاتورة تاريخية يعاد احتساب التكلفة المتوسطة، تكلفة المبيعات، المخزون والذمم داخل Transaction واحدة.",
                    size=11,
                    color=Colors.TEXT_SECONDARY,
                ),
                ft.Container(height=8),
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

        sticky_bar = ft.Container(
            ft.Column(
                [
                    paid_progress,
                    ft.Row(
                        [
                            ft.Column([ft.Text("إجمالي الفاتورة", size=12, color=Colors.TEXT_SECONDARY), total_text], spacing=2, expand=True),
                            ft.Column([ft.Text("المتبقي", size=12, color=Colors.TEXT_SECONDARY), remaining_text], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.END, expand=True),
                            ft.FilledButton(
                                "حفظ التعديلات" if self.editing_id else "حفظ الفاتورة",
                                icon=ft.Icons.SAVE_OUTLINED,
                                on_click=save,
                                height=46,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=8,
            ),
            padding=ft.padding.only(left=16, right=16, top=10, bottom=12),
            bgcolor=Colors.WHITE,
            border=ft.border.only(top=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.LG,
        )

        self.content.content = ft.Column([sticky_top, scroll_body, sticky_bar], spacing=0, expand=True)
        update_party_insight()
        refresh_suggestions()
        recalc()
