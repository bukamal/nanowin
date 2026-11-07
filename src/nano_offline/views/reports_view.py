from __future__ import annotations

import asyncio
import csv
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import flet as ft

from nano_offline.core.toast import toast

from nano_offline.components import DataGrid, GridColumn, SmartDateField, empty_state, metric_row
from nano_offline.components.print_profile import apply_print_profile, open_print_profile_dialog
from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.core import privacy
from nano_offline.core import reporting_settings
from nano_offline.services.forecast_service import MONTH_NAMES_AR


# (key, label, icon) for each report tab. Labels are the exact literal
# strings the report-picking UI shows (and what tools/phase4_flet_ui_
# contract_smoke_test.py checks for verbatim), now driving a chip switcher
# instead of a Dropdown -- same tap-to-select language as FinanceCenter's
# section chips (views/finance_view.py._section_nav) and DashboardCenter's
# period pills, instead of a lone dropdown that read as a different, older
# control language than the rest of the app.
_REPORT_TABS: list[tuple[str, str, str]] = [
    ("pnl", "قائمة الدخل والربحية", ft.Icons.QUERY_STATS_OUTLINED),
    ("profitability", "ربحية الفواتير والمواد", ft.Icons.WORKSPACE_PREMIUM_OUTLINED),
    ("inventory", "حركة وتقييم المخزون", ft.Icons.INVENTORY_2_OUTLINED),
    ("balances", "ذمم العملاء والموردين", ft.Icons.PEOPLE_ALT_OUTLINED),
    ("cash", "حركة الصندوق", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED),
    ("forecast", "التوقعات الموسمية", ft.Icons.INSIGHTS_OUTLINED),
]

# Maps an accent color token to its matching soft-background token, resolved
# fresh on every call (never cached at import time) so it stays correct
# whether light or dark mode is active -- same reasoning as the inline dict
# DashboardCenter._metric builds on every call instead of once at import.
def _badge_bg(accent: str) -> str:
    return {
        Colors.PRIMARY: Colors.PRIMARY_BG,
        Colors.SUCCESS: Colors.SUCCESS_BG,
        Colors.SUCCESS_ALT: Colors.SUCCESS_BG,
        Colors.WARNING: Colors.WARNING_BG,
        Colors.WARNING_DARK: Colors.WARNING_BG,
        Colors.DANGER: Colors.DANGER_BG,
        Colors.DANGER_DARK: Colors.DANGER_BG,
        Colors.PURPLE: Colors.PURPLE_BG,
        Colors.PURPLE_LIGHT: Colors.PURPLE_BG,
        Colors.ORANGE: Colors.WARNING_BG_ALT,
    }.get(accent, Colors.PRIMARY_BG)


class ReportsCenter:
    def __init__(self, page: ft.Page, ctx, content: ft.Container, *, native_files=None, on_title_change=None, on_open_statement=None, on_open_invoice=None, on_context_actions=None):
        self.page = page
        self.ctx = ctx
        self.content = content
        self.native_files = native_files
        self.on_title_change = on_title_change
        self.on_open_statement = on_open_statement
        self.on_open_invoice = on_open_invoice
        # Publishes the screen's primary actions into the shell's command
        # strip (under the tab), like the items/parties screens.
        self.on_context_actions = on_context_actions
        self._report_key: str = "pnl"
        self._refresh_generation = 0
        self.report_chips: dict[str, ft.Container] = {}
        self.date_from: ft.TextField | None = None
        self.date_to: ft.TextField | None = None
        self.date_preset: ft.Dropdown | None = None
        self.body = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)
        # NOTE: no wrap=True on this Row. Two fixed action buttons always
        # fit one line, and combining wrap=True with expand=True children
        # is exactly what used to crash this bar's render on Android --
        # Flutter's Wrap (what a wrapping ft.Row becomes) cannot host
        # Expanded/Flexible children, and the failed build painted as a
        # silent solid-gray box swallowing the report body underneath it
        # (see components/segmented_toggle.py's docstring for the same
        # class of bug and why SegmentedToggle deliberately never wraps).
        # That combination was the root cause of the reports tab's gray
        # screen -- this Row is the fix.

    def _set_header(self, title: str, subtitle: str = "") -> None:
        if self.on_title_change:
            self.on_title_change(title, subtitle)

    def money(self, value) -> str:
        return privacy.money_ctx(value, self.ctx)

    @staticmethod
    def number(value) -> str:
        return f"{float(value or 0):,.3f}".rstrip("0").rstrip(".")

    def notify(self, text: str, kind: str | None = None, sound_kind: str | None = None) -> None:
        toast(self.page, text, kind=kind, sound_kind=sound_kind)

    # -- shared building blocks ---------------------------------------------
    def _metric(self, title: str, value: str, *, icon, accent: str, note: str = "") -> ft.Container:
        return ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                ft.Icon(icon, size=19, color=accent),
                                width=38, height=38, alignment=ft.alignment.center,
                                bgcolor=_badge_bg(accent), border_radius=13,
                            ),
                            ft.Text(title, size=11.5, color=Colors.TEXT_SECONDARY, weight=ft.FontWeight.W_600, expand=True),
                        ],
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(value, size=18, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                    ft.Text(note, size=9.5, color=Colors.TEXT_FAINT) if note else ft.Container(height=1),
                ],
                spacing=7,
            ),
            padding=13,
            border=ft.border.all(1, Colors.BORDER_ALT),
            border_radius=Radius.LG,
            bgcolor=Colors.WHITE,
            shadow=Shadow.SM,
        )

    def _section_title(self, icon, text: str) -> ft.Row:
        return ft.Row(
            [
                ft.Icon(icon, size=17, color=Colors.PRIMARY),
                ft.Text(text, size=15.5, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
            ],
            spacing=7,
        )

    def _card(self, controls: list[ft.Control]) -> ft.Container:
        return ft.Container(
            ft.Column(controls, spacing=6),
            padding=12,
            border=ft.border.all(1, Colors.BORDER_ALT),
            border_radius=Radius.LG,
            bgcolor=Colors.WHITE,
            shadow=Shadow.SM,
        )

    def _entry(
        self, *, icon, accent: str, title: str, subtitle: str,
        value: str, value_sub: str | None = None, footer: ft.Control | None = None,
        on_click=None,
    ) -> ft.Container:
        """One list row shared by every report's item/invoice/party/movement
        list -- icon badge + title/subtitle + trailing value, same visual
        language as FinanceCenter's voucher rows, instead of each report
        drawing its own bespoke text-stack card."""
        rows: list[ft.Control] = [
            ft.Row(
                [
                    ft.Container(
                        ft.Icon(icon, size=17, color=accent),
                        width=40, height=40, alignment=ft.alignment.center,
                        bgcolor=_badge_bg(accent), border_radius=13,
                    ),
                    ft.Column(
                        [
                            ft.Text(title, size=12.5, weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(subtitle, size=10.5, color=Colors.TEXT_SECONDARY, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=2, expand=True,
                    ),
                    ft.Column(
                        [
                            ft.Text(value, size=13.5, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                            *([ft.Text(value_sub, size=9.5, color=Colors.TEXT_FAINT)] if value_sub else []),
                        ],
                        spacing=1, horizontal_alignment=ft.CrossAxisAlignment.END,
                    ),
                    *([ft.Icon(ft.Icons.CHEVRON_LEFT, size=18, color=Colors.TEXT_FAINT)] if on_click is not None else []),
                ],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        ]
        if footer is not None:
            rows.append(footer)
        return ft.Container(
            ft.Column(rows, spacing=6),
            padding=12,
            bgcolor=Colors.WHITE,
            border=ft.border.all(1, Colors.BORDER_ALT),
            border_radius=Radius.LG,
            shadow=Shadow.SM,
            on_click=on_click, ink=on_click is not None,
        )

    @staticmethod
    def _footnote(text: str) -> ft.Text:
        return ft.Text(text, size=11, color=Colors.TEXT_SECONDARY)

    def _readonly_grid(
        self,
        columns: list[GridColumn],
        rows: list[dict],
        *,
        on_row_activate=None,
        row_action_label: str = "فتح المرتبط",
        height: int = 340,
    ) -> ft.Control:
        grid = DataGrid(
            columns=columns,
            rows=rows,
            viewport_height=height,
            boxed=True,
            center_cells=True,
            on_row_activate=on_row_activate,
            on_row_menu=(
                lambda row: [
                    (row_action_label, ft.Icons.OPEN_IN_NEW_ROUNDED,
                     lambda selected=row: on_row_activate(selected))
                ]
                if on_row_activate is not None else []
            ),
            formatter=lambda col, value, _row: (
                self.money(value) if col.kind == "money"
                else f"{float(value or 0):,.1f}%" if col.key.endswith("_percent") or col.key == "margin_percent"
                else self.number(value) if col.kind == "number"
                else value
            ),
            on_sort=lambda key, direction: grid_sort(key, direction),
        )

        def grid_sort(key: str | None, direction: str | None) -> None:
            if not key:
                ordered = rows
            else:
                ordered = sorted(
                    rows,
                    key=lambda row: (row.get(key) is None, row.get(key) or 0),
                    reverse=direction == "desc",
                )
            grid.set_rows(ordered)

        return grid

    def _empty(self, text: str, *, icon=ft.Icons.INSIGHTS_OUTLINED) -> ft.Container:
        return empty_state(text, icon=icon)

    def _error_card(self, exc: Exception) -> ft.Container:
        """Inline failure state for a single report render -- keeps the
        picker/date bar usable and offers a retry instead of leaving the
        body empty (which used to just show bare page background and read
        as a mysterious gray gap) or silently only toasting the error."""
        message = str(exc).strip() or exc.__class__.__name__
        return ft.Container(
            ft.Column(
                [
                    ft.Icon(ft.Icons.ERROR_OUTLINE, size=34, color=Colors.DANGER_DARKER),
                    ft.Text("تعذر تحميل هذا التقرير", size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                    ft.Text("جرّب تحديث الفترة أو الضغط على إعادة المحاولة.", size=11, color=Colors.TEXT_SECONDARY, text_align=ft.TextAlign.CENTER),
                    ft.Text(message, size=10.5, color=Colors.DANGER_DARKER, selectable=True, text_align=ft.TextAlign.CENTER),
                    ft.FilledButton("إعادة المحاولة", icon=ft.Icons.REFRESH, on_click=self._refresh),
                ],
                spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=20,
            border=ft.border.all(1, Colors.DANGER_BORDER),
            border_radius=Radius.LG,
            bgcolor=Colors.DANGER_BG,
            alignment=ft.alignment.center,
        )

    _DONUT_PALETTE = [
        Colors.PRIMARY, Colors.PURPLE_LIGHT, Colors.ORANGE,
        Colors.WARNING, Colors.DANGER, Colors.SUCCESS_ALT,
    ]

    def _category_donut(self, rows: list[dict], *, label_key: str, value_key: str) -> ft.Control | None:
        """Donut breakdown for a list of {label_key, value_key} rows.

        Used for the expense-by-category breakdown in the P&L report, which
        was previously a text-only list — the categories with the largest
        share are now visible at a glance instead of requiring the reader to
        scan and mentally compare numbers.
        """
        positive = [r for r in rows if float(r.get(value_key) or 0) > 0]
        if not positive:
            return None
        total = sum(float(r[value_key]) for r in positive)
        sections = []
        legend = []
        for index, row in enumerate(sorted(positive, key=lambda r: -float(r[value_key]))[:6]):
            color = self._DONUT_PALETTE[index % len(self._DONUT_PALETTE)]
            value = float(row[value_key])
            pct = (value / total * 100) if total else 0
            sections.append(ft.PieChartSection(value=value, color=color, radius=11))
            legend.append(
                ft.Row(
                    [
                        ft.Container(width=10, height=10, bgcolor=color, border_radius=3),
                        ft.Text(str(row[label_key]), size=11, expand=True),
                        ft.Text(f"{pct:.0f}%", size=11, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=6,
                )
            )
        return ft.Row(
            [
                ft.Container(
                    ft.PieChart(sections=sections, sections_space=2, center_space_radius=22, height=110, width=110),
                    width=110, height=110,
                ),
                ft.Column(legend, spacing=6, expand=True),
            ],
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    # -- report tab chips -----------------------------------------------------
    def _select_report(self, key: str) -> None:
        if key == self._report_key:
            return
        self._report_key = key
        for chip_key, chip in self.report_chips.items():
            active = chip_key == key
            chip.bgcolor = Colors.PRIMARY if active else Colors.WHITE
            chip.border = ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER)
            row: ft.Row = chip.content
            row.controls[0].color = Colors.WHITE if active else Colors.TEXT_MUTED
            row.controls[1].color = Colors.WHITE if active else Colors.TEXT_MUTED
            row.controls[1].weight = ft.FontWeight.BOLD if active else ft.FontWeight.W_500
        self._refresh()

    # -- screen shell ---------------------------------------------------------
    def show_center(self) -> None:
        """Public entry point -- kept crash-proof end to end. Anything that
        goes wrong while *building* the screen (not just while rendering a
        report's data, handled separately by ``_refresh``) now falls back to
        a recoverable in-app error card instead of leaving a raw Flutter
        build failure on screen, same defensive pattern InvoiceCenter uses
        (views/invoice_view.py's ``_show_center_error``)."""
        try:
            self._build_center()
        except Exception as exc:
            self._show_center_error(exc)

    def _show_center_error(self, exc: Exception) -> None:
        self._set_header("التقارير", "تعذر تحميل شاشة التقارير")
        message = str(exc).strip() or exc.__class__.__name__
        self.content.content = ft.Column(
            [
                ft.Container(
                    ft.Column(
                        [
                            ft.Icon(ft.Icons.ERROR_OUTLINE, size=42, color=Colors.DANGER_DARKER),
                            ft.Text("تعذر تحميل شاشة التقارير", size=18, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                "تم منع انهيار الواجهة. أعد المحاولة، وإذا تكرر الخطأ فاحتفظ بالتفاصيل التالية.",
                                size=12, color=Colors.TEXT_SECONDARY, text_align=ft.TextAlign.CENTER,
                            ),
                            ft.Text(message, size=11, color=Colors.DANGER_DARKER, selectable=True),
                            ft.FilledButton("إعادة المحاولة", icon=ft.Icons.REFRESH, on_click=lambda _: self.show_center()),
                        ],
                        spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=20,
                    border=ft.border.all(1, Colors.DANGER_BORDER),
                    border_radius=Radius.LG,
                    bgcolor=Colors.DANGER_BG,
                ),
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
        )
        self.page.update()

    def _build_center(self) -> None:
        self._set_header("التقارير", "التقارير المالية والمخزون والربحية")
        self._report_key = reporting_settings.default_report(self.ctx.settings)
        _default_from, _default_to = reporting_settings.default_range_dates(self.ctx.settings)
        self.date_from = SmartDateField(label="من تاريخ YYYY-MM-DD", hint_text="اتركه فارغًا لكل الفترة", value=_default_from)
        self.date_to = SmartDateField(label="إلى تاريخ / كما في YYYY-MM-DD", hint_text="اتركه فارغًا حتى آخر حركة", value=_default_to)
        presets = [
            ("today", "اليوم"),
            ("week", "هذا الأسبوع"),
            ("month", "هذا الشهر"),
            ("quarter", "هذا الربع"),
            ("year", "هذه السنة"),
            ("previous_month", "الشهر السابق"),
            ("all", "كل الفترة"),
        ]
        self.date_preset = ft.Dropdown(
            label="الفترة",
            value=None,
            options=[ft.dropdown.Option(key=key, text=label) for key, label in presets],
            width=165,
            filled=True,
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.MD,
            border_color=Colors.BORDER,
        )
        self.date_preset.on_change = lambda e: self._apply_date_preset(e.control.value)

        self.report_chips = {}
        report_picker = ft.Row(
            [
                self._report_chip(key, label, icon)
                for key, label, icon in _REPORT_TABS
            ],
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
        )

        # Report type buttons live directly under the page title; date,
        # preset, refresh and export controls share one toolbar row.
        sticky_top = ft.Container(
            ft.Column(
                [
                    report_picker,
                    ft.Row(
                        [
                            ft.Container(self.date_from, width=190),
                            ft.Container(self.date_to, width=190),
                            self.date_preset,
                            ft.IconButton(
                                icon=ft.Icons.REFRESH,
                                tooltip="تحديث التقرير",
                                on_click=self._refresh,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.PRINT_OUTLINED,
                                tooltip="طباعة التقرير",
                                on_click=lambda _: self.page.run_task(self._print_report),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.PICTURE_AS_PDF_OUTLINED,
                                tooltip="مشاركة PDF",
                                on_click=lambda _: self.page.run_task(self._share_report_pdf),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DOWNLOAD_OUTLINED,
                                tooltip="تصدير CSV",
                                on_click=lambda _: self.page.run_task(self._export_csv),
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=12,
            ),
            padding=ft.padding.only(left=18, right=18, top=14, bottom=14),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
        )

        if self.on_context_actions is not None:
            self.on_context_actions([])

        self.content.content = ft.Column(
            [
                sticky_top,
                ft.Container(
                    self.body,
                    padding=ft.padding.only(left=18, right=18, top=14, bottom=22),
                    expand=True,
                ),
            ],
            spacing=0,
            expand=True,
        )
        self._refresh()

    def _apply_date_preset(self, key: str) -> None:
        if not key:
            return
        today = date.today()
        if key == "today":
            start = end = today
        elif key == "week":
            start, end = today - timedelta(days=today.weekday()), today
        elif key == "month":
            start, end = today.replace(day=1), today
        elif key == "quarter":
            quarter_start_month = ((today.month - 1) // 3) * 3 + 1
            start, end = today.replace(month=quarter_start_month, day=1), today
        elif key == "year":
            start, end = today.replace(month=1, day=1), today
        elif key == "previous_month":
            end = today.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
        else:
            start = end = None
        self.date_from.value = start.isoformat() if start else ""
        self.date_to.value = end.isoformat() if end else ""
        self._refresh()

    def _report_chip(self, key: str, label: str, icon: str) -> ft.Container:
        active = key == self._report_key
        chip = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(icon, size=15, color=Colors.WHITE if active else Colors.TEXT_MUTED),
                    ft.Text(
                        label,
                        size=12,
                        color=Colors.WHITE if active else Colors.TEXT_MUTED,
                        weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_500,
                        max_lines=1,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=13, vertical=9),
            bgcolor=Colors.PRIMARY if active else Colors.WHITE,
            border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
            border_radius=20,
            ink=True,
            animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
            on_click=lambda _, report_key=key: self._select_report(report_key),
        )
        self.report_chips[key] = chip
        return chip

    def _dates(self) -> tuple[str | None, str | None]:
        return (
            (self.date_from.value or "").strip() or None,
            (self.date_to.value or "").strip() or None,
        )

    def _refresh(self, _=None) -> None:
        report = self._report_key or "pnl"
        date_from, date_to = self._dates()
        self._refresh_generation += 1
        generation = self._refresh_generation
        self.body.controls = [ft.Container(ft.ProgressRing(), padding=24, alignment=ft.alignment.center)]
        self.page.update()
        self.page.run_task(self._load_and_render, generation, report, date_from, date_to)

    async def _load_report_data(self, report: str, date_from: str | None, date_to: str | None):
        reports = self.ctx.reports.aio
        if report == "pnl":
            tasks = [
                reports.income_statement(date_from=date_from, date_to=date_to),
                reports.profit_trend(date_from=date_from, date_to=date_to),
            ]
            if date_from and date_to:
                tasks.append(reports.period_comparison(date_from=date_from, date_to=date_to))
            else:
                tasks.append(asyncio.sleep(0, result=None))
            return await asyncio.gather(*tasks)
        if report == "profitability":
            invoices, items = await asyncio.gather(
                reports.invoice_profitability(date_from=date_from, date_to=date_to),
                reports.item_profitability(date_from=date_from, date_to=date_to),
            )
            top = sorted(items, key=lambda row: float(row.get("revenue") or 0), reverse=True)[:5]
            return invoices, items, top
        if report == "inventory":
            return await asyncio.gather(
                reports.inventory_report(date_from=date_from, date_to=date_to),
                reports.inventory_valuation(as_of=date_to),
            )
        if report == "balances":
            return await asyncio.gather(
                reports.party_balances("customer", as_of=date_to),
                reports.party_balances("supplier", as_of=date_to),
                reports.outstanding_invoices("customer", as_of=date_to),
                reports.outstanding_invoices("supplier", as_of=date_to),
                reports.aging_summary("customer", as_of=date_to),
                reports.aging_summary("supplier", as_of=date_to),
            )
        if report == "forecast":
            return await asyncio.gather(
                self.ctx.forecast.aio.seasonal_forecast(invoice_type="sale", date_from=date_from, date_to=date_to),
                self.ctx.forecast.aio.seasonal_forecast(invoice_type="purchase", date_from=date_from, date_to=date_to),
                self.ctx.forecast.aio.forecast_accuracy(invoice_type="sale", date_from=date_from, date_to=date_to),
            )
        if report == "cash":
            return await asyncio.gather(
                reports.cash_movement(date_from=date_from, date_to=date_to),
                reports.cash_outlook(as_of=date_to),
            )
        return await reports.cash_movement(date_from=date_from, date_to=date_to)

    async def _load_and_render(self, generation: int, report: str, date_from: str | None, date_to: str | None) -> None:
        try:
            data = await self._load_report_data(report, date_from, date_to)
            if generation != self._refresh_generation:
                return
            if report == "pnl":
                controls = self._render_pnl(*data)
            elif report == "profitability":
                controls = self._render_profitability(*data)
            elif report == "inventory":
                controls = self._render_inventory(*data)
            elif report == "balances":
                controls = self._render_balances(*data)
            elif report == "forecast":
                controls = self._render_forecast(*data)
            elif report == "cash":
                controls = self._render_cash(*data)
            else:
                controls = self._render_balances(*data)
        except Exception as exc:
            if generation != self._refresh_generation:
                return
            self.notify(str(exc), kind="error")
            controls = [self._error_card(exc)]
        self.body.controls = controls
        self.page.update()

    async def _report_html(self) -> str:
        """Build the printable HTML for whichever report is on screen now,
        re-reading the same data the cards were just drawn from -- so print
        and PDF share always match what the user is currently looking at.
        """
        report = self._report_key or "pnl"
        date_from, date_to = self._dates()
        documents = self.ctx.documents
        reports = self.ctx.reports.aio
        if report == "pnl":
            data, trend = await asyncio.gather(
                reports.income_statement(date_from=date_from, date_to=date_to),
                reports.profit_trend(date_from=date_from, date_to=date_to),
            )
            comparison = (
                await reports.period_comparison(date_from=date_from, date_to=date_to)
                if date_from and date_to else None
            )
            return documents.pnl_report_html(
                data, date_from=date_from, date_to=date_to, trend=trend, comparison=comparison
            )
        if report == "profitability":
            invoices, items = await asyncio.gather(
                reports.invoice_profitability(date_from=date_from, date_to=date_to),
                reports.item_profitability(date_from=date_from, date_to=date_to),
            )
            top = sorted(items, key=lambda row: float(row.get("revenue") or 0), reverse=True)[:5]
            return documents.profitability_report_html(
                invoices=invoices, items=items, top=top, date_from=date_from, date_to=date_to
            )
        if report == "inventory":
            rows, valuation = await asyncio.gather(
                reports.inventory_report(date_from=date_from, date_to=date_to),
                reports.inventory_valuation(as_of=date_to),
            )
            return documents.inventory_report_html(rows=rows, valuation=valuation, date_from=date_from, date_to=date_to)
        if report == "balances":
            customers, suppliers, open_customers, open_suppliers, customer_aging, supplier_aging = await asyncio.gather(
                reports.party_balances("customer", as_of=date_to),
                reports.party_balances("supplier", as_of=date_to),
                reports.outstanding_invoices("customer", as_of=date_to),
                reports.outstanding_invoices("supplier", as_of=date_to),
                reports.aging_summary("customer", as_of=date_to),
                reports.aging_summary("supplier", as_of=date_to),
            )
            return documents.balances_report_html(
                customers=customers, suppliers=suppliers,
                open_customers=open_customers, open_suppliers=open_suppliers, as_of=date_to,
                customer_aging=customer_aging, supplier_aging=supplier_aging,
            )
        if report == "forecast":
            sales, purchases, accuracy = await asyncio.gather(
                self.ctx.forecast.aio.seasonal_forecast(invoice_type="sale", date_from=date_from, date_to=date_to),
                self.ctx.forecast.aio.seasonal_forecast(invoice_type="purchase", date_from=date_from, date_to=date_to),
                self.ctx.forecast.aio.forecast_accuracy(invoice_type="sale", date_from=date_from, date_to=date_to),
            )
            return documents.forecast_report_html(sales=sales, purchases=purchases, accuracy=accuracy, date_from=date_from, date_to=date_to)
        data, outlook = await asyncio.gather(
            reports.cash_movement(date_from=date_from, date_to=date_to),
            reports.cash_outlook(as_of=date_to),
        )
        return documents.cash_report_html(data, outlook=outlook, date_from=date_from, date_to=date_to)

    async def _export_csv(self) -> None:
        if self.native_files is None:
            self.notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
            return
        report = self._report_key or "pnl"
        date_from, date_to = self._dates()
        try:
            data = await self._load_report_data(report, date_from, date_to)
            if report == "pnl":
                summary, _trend, _comparison = data
                headers = ["القسم", "المؤشر", "القيمة", "النسبة"]
                rows = [
                    ["قائمة الدخل", "المبيعات", summary["sales"], ""],
                    ["قائمة الدخل", "تكلفة المبيعات", summary["cogs"], ""],
                    ["قائمة الدخل", "مجمل الربح", summary["gross_profit"], summary["gross_margin_percent"]],
                    ["قائمة الدخل", "المصروفات", summary["expenses"], ""],
                    ["قائمة الدخل", "صافي الربح", summary["net_profit"], summary["net_margin_percent"]],
                    ["قائمة الدخل", "المشتريات الفعلية", summary["purchases"], ""],
                    ["قائمة الدخل", "مخزون أول المدة", summary["opening_inventory"], ""],
                    *[["مصروفات", row["category"], row["amount"], ""] for row in summary["expense_breakdown"]],
                ]
            elif report == "profitability":
                invoices, items, _top = data
                headers = ["النوع", "المعرف/الاسم", "التاريخ", "الطرف/النوع", "المبيعات", "التكلفة", "الربح", "هامش الربح %", "الزيادة على التكلفة %"]
                rows = [
                    ["فاتورة", row["id"], row["invoice_date"], row["customer_name"], row["total"], row["cogs"], row["gross_profit"], row["margin_percent"], row["markup_percent"]]
                    for row in invoices
                ] + [
                    ["مادة/خدمة", row["item_name"], "", row["item_type"], row["revenue"], row["cogs"], row["gross_profit"], row["margin_percent"], row["markup_percent"]]
                    for row in items
                ]
            elif report == "inventory":
                items, _valuation = data
                headers = ["المادة", "الوحدة", "افتتاحي", "وارد", "صادر", "ختامي", "متوسط التكلفة", "قيمة المخزون"]
                rows = [[row["name"], row["unit_name"], row["opening_quantity_period"], row["purchases_quantity"], row["sales_quantity"], row["closing_quantity"], row["closing_unit_cost"], row["closing_value"]] for row in items]
            elif report == "balances":
                customers, suppliers, open_customers, open_suppliers, _customer_aging, _supplier_aging = data
                headers = ["النوع", "الطرف", "الفاتورة", "التاريخ", "العمر بالأيام", "شريحة العمر", "الرصيد/المتبقي"]
                rows = [
                    [kind, row.get("name") or row.get("party_name"), row.get("id", ""), row.get("invoice_date", ""), row.get("age_days", ""), row.get("age_bucket", ""), row.get("balance", row.get("remaining_amount", 0))]
                    for kind, report_data, invoices_data in (
                        ("عميل", customers, open_customers),
                        ("مورد", suppliers, open_suppliers),
                    )
                    for row in (report_data["rows"] + invoices_data)
                ]
            elif report == "cash":
                cash, outlook = data
                headers = ["التاريخ", "البيان", "النوع", "قبض", "صرف", "الرصيد"]
                rows = [[row["entry_date"], row.get("description") or row["source_type"], row["source_type"], row["debit"], row["credit"], row["balance"]] for row in cash["rows"]]
                rows.extend([["سيناريو", f"رصيد بعد {row['days']} يوم", "", "", "", row["balance"]] for row in outlook["projections"]])
            else:
                sales, purchases, accuracy = data
                headers = ["النوع", "الشهر", "فعلي", "توقع", "حد أدنى", "حد أعلى", "مؤشر موسمي"]
                rows = [
                    [kind, row["month"], row["total"], "", "", "", ""]
                    for kind, forecast in (("مبيعات تاريخية", sales), ("مشتريات تاريخية", purchases))
                    for row in forecast.get("history", [])
                ] + [
                    [kind, row["month"], "", row["forecast"], row["low"], row["high"], row["seasonal_index"]]
                    for kind, forecast in (("توقع مبيعات", sales), ("توقع مشتريات", purchases))
                    for row in forecast.get("forecasts", [])
                ]
                if not accuracy["insufficient"]:
                    rows.append(["اختبار رجعي", "WAPE %", "", accuracy["wape_percent"], "", "", ""])
            if not rows:
                self.notify("لا توجد بيانات للتصدير", kind="warning")
                return
            path = Path(tempfile.gettempdir()) / f"nano_report_{report}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(headers)
                writer.writerows(rows)
            await self.native_files.share_file(str(path), mime_type="text/csv", subject="تصدير تقرير")
            self.notify("تم تجهيز ملف CSV وفتحه للمشاركة")
        except Exception as exc:
            self.notify(str(exc), kind="error")

    async def _print_report(self, _=None) -> None:
        if self.native_files is None:
            self.notify("الطباعة الأصلية غير مهيأة في هذا البناء")
            return

        async def print_selected(layout: str, width: int) -> None:
            try:
                html = apply_print_profile(await self._report_html(), layout, width)
                report = self._report_key or "pnl"
                await self.native_files.print_html(
                    html,
                    name=f"nano-report-{report}",
                    paper_layout=layout,
                    roll_width_mm=width,
                )
            except Exception as exc:
                self.notify(str(exc), kind="error")

        open_print_profile_dialog(
            self.page,
            self.ctx.settings,
            print_selected,
            surface="report",
            native_files=self.native_files,
            preview_builder=lambda _layout, _width: self._report_html(),
        )

    async def _share_report_pdf(self, _=None) -> None:
        if self.native_files is None:
            self.notify("تصدير PDF غير مهيأ في هذا البناء")
            return

        async def share_selected(layout: str, width: int) -> None:
            try:
                html = apply_print_profile(await self._report_html(), layout, width)
                report = self._report_key or "pnl"
                await self.native_files.share_pdf(
                    html,
                    filename=f"nano_report_{report}.pdf",
                    paper_layout=layout,
                    roll_width_mm=width,
                )
            except Exception as exc:
                self.notify(str(exc), kind="error")

        open_print_profile_dialog(
            self.page,
            self.ctx.settings,
            share_selected,
            title="تنسيق ملف PDF",
            surface="report",
            native_files=self.native_files,
            preview_builder=lambda _layout, _width: self._report_html(),
        )

    def _comparison_metric(self, title: str, values: dict, *, percent: bool = False) -> ft.Container:
        delta = float(values["difference"])
        change = values["change_percent"]
        value = f"{delta:+.1f} نقطة" if percent else self.money(delta)
        note = "لا توجد قاعدة مقارنة" if change is None else f"{float(change):+.1f}% عن الفترة السابقة"
        return self._metric(
            title,
            value,
            icon=ft.Icons.TRENDING_UP_ROUNDED if delta >= 0 else ft.Icons.TRENDING_DOWN_ROUNDED,
            accent=Colors.SUCCESS if delta >= 0 else Colors.DANGER,
            note=note,
        )

    def _trend_bars(self, rows: list[dict]) -> ft.Control:
        if not rows:
            return self._empty("لا توجد بيانات كافية لعرض الاتجاه.", icon=ft.Icons.SHOW_CHART)
        max_sales = max((float(row["sales"]) for row in rows), default=0.0) or 1.0
        bars = []
        for row in rows[-12:]:
            sales_width = max(2, int(190 * float(row["sales"]) / max_sales)) if row["sales"] > 0 else 0
            profit_width = max(2, int(190 * abs(float(row["net_profit"])) / max_sales)) if row["net_profit"] else 0
            bars.append(ft.Row(
                [
                    ft.Text(str(row["month"]), width=68, size=11),
                    ft.Column([
                        ft.Row([ft.Container(width=sales_width, height=8, bgcolor=Colors.PRIMARY, border_radius=4)]),
                        ft.Row([ft.Container(width=profit_width, height=8, bgcolor=Colors.SUCCESS if row["net_profit"] >= 0 else Colors.DANGER, border_radius=4)]),
                    ], spacing=4, expand=True),
                    ft.Column([
                        ft.Text(f"مبيعات {self.money(row['sales'])}", size=10),
                        ft.Text(f"صافي {self.money(row['net_profit'])}", size=10, color=Colors.TEXT_SECONDARY),
                    ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.END),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))
        return self._card([
            ft.Row([ft.Text("المبيعات", color=Colors.PRIMARY), ft.Text("صافي الربح", color=Colors.SUCCESS)], spacing=14),
            *bars,
        ])

    def _render_pnl(self, report: dict, trend: list[dict], comparison: dict | None) -> list[ft.Control]:
        metrics = metric_row([
            self._metric("المبيعات", self.money(report["sales"]), icon=ft.Icons.POINT_OF_SALE_OUTLINED, accent=Colors.PRIMARY),
            self._metric("تكلفة المبيعات", self.money(report["cogs"]), icon=ft.Icons.RECEIPT_LONG_OUTLINED, accent=Colors.ORANGE),
            self._metric("مجمل الربح", self.money(report["gross_profit"]), icon=ft.Icons.TRENDING_UP_ROUNDED, accent=Colors.SUCCESS, note=f"هامش {report['gross_margin_percent']:.1f}% من المبيعات"),
            self._metric("المصروفات", self.money(report["expenses"]), icon=ft.Icons.PAYMENTS_OUTLINED, accent=Colors.DANGER),
            self._metric("صافي الربح", self.money(report["net_profit"]), icon=ft.Icons.SAVINGS_OUTLINED, accent=Colors.SUCCESS_ALT, note=f"هامش صافي {report['net_margin_percent']:.1f}%"),
            self._metric("المشتريات", self.money(report["purchases"]), icon=ft.Icons.ADD_SHOPPING_CART_OUTLINED, accent=Colors.PURPLE_LIGHT, note="مشتريات فعلية خلال الفترة"),
        ], spacing=10)
        expenses = self._readonly_grid(
            [
                GridColumn("category", "التصنيف", width=180),
                GridColumn("count", "الحركات", kind="number", width=95),
                GridColumn("amount", "المبلغ", kind="money", width=150),
            ],
            report["expense_breakdown"],
            height=240,
        )
        result: list[ft.Control] = [
            metrics,
            metric_row([
                self._metric("هامش مجمل الربح", f"{report['gross_margin_percent']:.1f}%", icon=ft.Icons.PERCENT, accent=Colors.SUCCESS, note="مجمل الربح ÷ المبيعات"),
                self._metric("هامش صافي الربح", f"{report['net_margin_percent']:.1f}%", icon=ft.Icons.SAVINGS_OUTLINED, accent=Colors.PRIMARY, note="بعد المصروفات"),
                self._metric("الزيادة على التكلفة", f"{report['markup_percent']:.1f}%", icon=ft.Icons.CALCULATE_OUTLINED, accent=Colors.PURPLE, note="الربح ÷ تكلفة المبيعات"),
                self._metric("مخزون أول المدة", self.money(report["opening_inventory"]), icon=ft.Icons.INVENTORY_2_OUTLINED, accent=Colors.ORANGE, note="رصيد افتتاحي، ليس شراءً للفترة"),
            ], spacing=10),
        ]
        if comparison is not None:
            metrics_change = comparison["metrics"]
            result.extend([
                self._section_title(ft.Icons.COMPARE_ARROWS, "مقارنة بالفترة السابقة"),
                self._footnote(f"{comparison['date_from']} إلى {comparison['date_to']} مقابل {comparison['previous_from']} إلى {comparison['previous_to']}"),
                metric_row([
                    self._comparison_metric("المبيعات", metrics_change["sales"]),
                    self._comparison_metric("صافي الربح", metrics_change["net_profit"]),
                    self._comparison_metric("هامش صافي الربح", metrics_change["net_margin_percent"], percent=True),
                ], spacing=10),
            ])
        result.extend([
            self._section_title(ft.Icons.SHOW_CHART, "اتجاه الأداء الشهري"),
            self._trend_bars(trend),
            self._section_title(ft.Icons.CATEGORY_OUTLINED, "المصروفات حسب التصنيف"),
            expenses,
        ])
        donut = self._category_donut(report["expense_breakdown"], label_key="category", value_key="amount")
        if donut is not None:
            result.append(self._card([donut]))
        return result

    def _render_profitability(self, invoices: list[dict], items: list[dict], top: list[dict]) -> list[ft.Control]:
        top_grid = self._readonly_grid(
            [
                GridColumn("item_name", "المادة", width=180),
                GridColumn("quantity_in_base", "الكمية", kind="number", width=100),
                GridColumn("revenue", "المبيعات", kind="money", width=140),
                GridColumn("gross_profit", "الربح", kind="money", width=140),
                GridColumn("margin_percent", "الهامش", width=110),
            ],
            top,
            height=280,
        )
        invoice_grid = self._readonly_grid(
            [
                GridColumn("id", "الفاتورة", kind="number", width=85),
                GridColumn("invoice_date", "التاريخ", width=110),
                GridColumn("customer_name", "العميل", width=150),
                GridColumn("total", "المبيعات", kind="money", width=130),
                GridColumn("cogs", "التكلفة", kind="money", width=130),
                GridColumn("gross_profit", "الربح", kind="money", width=130),
                GridColumn("margin_percent", "الهامش", width=100),
                GridColumn("markup_percent", "الزيادة", width=100),
            ],
            invoices,
            on_row_activate=lambda row: self.on_open_invoice(int(row["id"])) if self.on_open_invoice else None,
            height=360,
        )
        item_grid = self._readonly_grid(
            [
                GridColumn("item_name", "المادة/الخدمة", width=180),
                GridColumn("item_type", "النوع", width=90),
                GridColumn("quantity_in_base", "الكمية", kind="number", width=100),
                GridColumn("revenue", "المبيعات", kind="money", width=130),
                GridColumn("cogs", "التكلفة", kind="money", width=130),
                GridColumn("gross_profit", "الربح", kind="money", width=130),
                GridColumn("margin_percent", "الهامش", width=100),
                GridColumn("markup_percent", "الزيادة", width=100),
                GridColumn("invoice_count", "فواتير", kind="number", width=80),
            ],
            items,
            height=360,
        )
        return [
            self._section_title(ft.Icons.WORKSPACE_PREMIUM_ROUNDED, "الأكثر مبيعًا (مرتبة بالإيراد)"), top_grid,
            self._section_title(ft.Icons.RECEIPT_LONG_OUTLINED, "ربحية الفواتير — اضغط صفًا لفتح الفاتورة"), invoice_grid,
            self._section_title(ft.Icons.INVENTORY_2_OUTLINED, "ربحية المواد والخدمات"), item_grid,
        ]

    def _render_inventory(self, rows: list[dict], valuation: dict) -> list[ft.Control]:
        _, date_to = self._dates()
        grid = self._readonly_grid(
            [
                GridColumn("name", "المادة", width=170),
                GridColumn("unit_name", "الوحدة", width=90),
                GridColumn("opening_quantity_period", "افتتاحي", kind="number", width=95),
                GridColumn("purchases_quantity", "وارد", kind="number", width=90),
                GridColumn("sales_quantity", "صادر", kind="number", width=90),
                GridColumn("closing_quantity", "ختامي", kind="number", width=90),
                GridColumn("closing_unit_cost", "متوسط التكلفة", kind="money", width=125),
                GridColumn("closing_value", "قيمة المخزون", kind="money", width=135),
            ],
            rows,
            height=440,
        )
        return [
            metric_row([
                self._metric("قيمة المخزون", self.money(valuation["total_value"]), icon=ft.Icons.INVENTORY_2_OUTLINED, accent=Colors.PRIMARY, note=f"كما في {date_to or 'آخر حركة'}"),
                self._metric("عدد المواد المخزنية", str(valuation["item_count"]), icon=ft.Icons.CATEGORY_OUTLINED, accent=Colors.WARNING),
            ], spacing=10),
            self._section_title(ft.Icons.SYNC_ALT_ROUNDED, "حركة المخزون حسب المادة"),
            grid,
        ]

    def _render_balances(
        self,
        customers: dict,
        suppliers: dict,
        open_customers: list[dict],
        open_suppliers: list[dict],
        customer_aging: dict,
        supplier_aging: dict,
    ) -> list[ft.Control]:
        def party_grid(report: dict, party_type: str) -> ft.Control:
            def open_party(row: dict) -> None:
                if self.on_open_statement is not None:
                    self.on_open_statement(party_type, int(row["id"]))
            return self._readonly_grid(
                [
                    GridColumn("name", "الاسم", width=180),
                    GridColumn("balance", "الرصيد (+مستحق / −دائن)", kind="money", width=190),
                    GridColumn("invoice_count", "الفواتير", kind="number", width=100),
                ],
                report["rows"],
                on_row_activate=open_party if self.on_open_statement is not None else None,
                height=300,
            )

        def invoice_grid(rows: list[dict]) -> ft.Control:
            return self._readonly_grid(
                [
                    GridColumn("id", "الفاتورة", kind="number", width=80),
                    GridColumn("party_name", "الطرف", width=160),
                    GridColumn("invoice_date", "التاريخ", width=110),
                    GridColumn("age_days", "العمر/يوم", kind="number", width=100),
                    GridColumn("age_bucket", "شريحة العمر", width=100),
                    GridColumn("total", "الإجمالي", kind="money", width=130),
                    GridColumn("paid_as_of", "المدفوع", kind="money", width=130),
                    GridColumn("remaining_amount", "المتبقي", kind="money", width=130),
                ],
                rows,
                on_row_activate=lambda row: self.on_open_invoice(int(row["id"])) if self.on_open_invoice else None,
                height=340,
            )

        def aging_metrics(aging: dict, title: str) -> list[ft.Control]:
            return [
                self._section_title(ft.Icons.DATE_RANGE_OUTLINED, title),
                self._footnote("العمر محسوب من تاريخ الفاتورة؛ لا يمثل تاريخ استحقاق تعاقديًا."),
                metric_row([
                    self._metric("0–30 يوم", self.money(aging["buckets"]["0-30"]), icon=ft.Icons.SCHEDULE, accent=Colors.SUCCESS),
                    self._metric("31–60 يوم", self.money(aging["buckets"]["31-60"]), icon=ft.Icons.SCHEDULE, accent=Colors.WARNING),
                    self._metric("61–90 يوم", self.money(aging["buckets"]["61-90"]), icon=ft.Icons.SCHEDULE, accent=Colors.ORANGE),
                    self._metric("أكثر من 90", self.money(aging["buckets"]["90+"]), icon=ft.Icons.WARNING_AMBER, accent=Colors.DANGER),
                ], spacing=10),
            ]
        return [
            metric_row([
                self._metric("ذمم العملاء", self.money(customers["positive_total"]), icon=ft.Icons.PEOPLE_OUTLINE, accent=Colors.WARNING),
                self._metric("أرصدة دائنة للعملاء", self.money(customers["credit_total"]), icon=ft.Icons.SAVINGS_OUTLINED, accent=Colors.SUCCESS),
                self._metric("ذمم الموردين", self.money(suppliers["positive_total"]), icon=ft.Icons.LOCAL_SHIPPING_OUTLINED, accent=Colors.DANGER),
                self._metric("دفعات مقدمة للموردين", self.money(suppliers["credit_total"]), icon=ft.Icons.PAYMENTS_OUTLINED, accent=Colors.PRIMARY),
            ], spacing=10),
            *aging_metrics(customer_aging, "أعمار ذمم العملاء"),
            self._section_title(ft.Icons.PEOPLE_ALT_OUTLINED, "العملاء — اضغط صفًا لفتح كشف الحساب"),
            party_grid(customers, "customer"),
            self._section_title(ft.Icons.RECEIPT_LONG_OUTLINED, "فواتير العملاء المفتوحة — اضغط صفًا لفتح الفاتورة"),
            invoice_grid(open_customers),
            *aging_metrics(supplier_aging, "أعمار ذمم الموردين"),
            self._section_title(ft.Icons.LOCAL_SHIPPING_OUTLINED, "الموردون — اضغط صفًا لفتح كشف الحساب"),
            party_grid(suppliers, "supplier"),
            self._section_title(ft.Icons.RECEIPT_LONG_OUTLINED, "فواتير الموردين المفتوحة — اضغط صفًا لفتح الفاتورة"),
            invoice_grid(open_suppliers),
        ]

    def _render_cash(self, report: dict, outlook: dict) -> list[ft.Control]:
        cash_grid = self._readonly_grid(
            [
                GridColumn("entry_date", "التاريخ", width=110),
                GridColumn("description", "البيان", width=210),
                GridColumn("source_type", "النوع", width=100),
                GridColumn("debit", "قبض", kind="money", width=130),
                GridColumn("credit", "صرف", kind="money", width=130),
                GridColumn("balance", "الرصيد بعد الحركة", kind="money", width=160),
            ],
            list(reversed(report["rows"])),
            height=420,
        )
        return [
            metric_row([
                self._metric("رصيد افتتاحي", self.money(report["opening_balance"]), icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, accent=Colors.PRIMARY),
                self._metric("المقبوضات", self.money(report["receipts"]), icon=ft.Icons.SOUTH_WEST_ROUNDED, accent=Colors.SUCCESS),
                self._metric("المدفوعات", self.money(report["payments"]), icon=ft.Icons.NORTH_EAST_ROUNDED, accent=Colors.DANGER),
                self._metric("الرصيد الختامي", self.money(report["closing_balance"]), icon=ft.Icons.ACCOUNT_BALANCE_OUTLINED, accent=Colors.PRIMARY),
            ], spacing=10),
            self._section_title(ft.Icons.AUTO_GRAPH, "سيناريو السيولة"),
            self._footnote(f"استقراء آلي لمتوسط صافي الحركة اليومية خلال آخر {outlook['lookback_days']} يومًا؛ ليس توقعًا للذمم المستحقة ولا يضمن التحصيل."),
            metric_row([
                self._metric("المتوسط اليومي الداخل", self.money(outlook["average_daily_receipts"]), icon=ft.Icons.SOUTH_WEST_ROUNDED, accent=Colors.SUCCESS),
                self._metric("المتوسط اليومي الخارج", self.money(outlook["average_daily_payments"]), icon=ft.Icons.NORTH_EAST_ROUNDED, accent=Colors.DANGER),
                *[
                    self._metric(f"رصيد سيناريو {row['days']} يوم", self.money(row["balance"]), icon=ft.Icons.ACCOUNT_BALANCE_OUTLINED, accent=Colors.PRIMARY)
                    for row in outlook["projections"]
                ],
            ], spacing=10),
            self._section_title(ft.Icons.SWAP_VERT_ROUNDED, "حركات الصندوق"),
            cash_grid,
        ]

    # -- seasonal forecast (phase10 wave4, B1) -----------------------------
    def _render_forecast(self, sales: dict, purchases: dict, accuracy: dict) -> list[ft.Control]:
        """Sales/purchase forecast built from local invoice history only.
        Pure-DB analytics (no native extension): renders the same data the
        printable HTML mirrors in document_service.forecast_report_html."""
        def direction_card(title: str, icon, accent: str, data: dict) -> list[ft.Control]:
            if data["insufficient"]:
                return [
                    self._section_title(icon, title),
                    self._empty(data.get("reason") or "لا توجد بيانات كافية للتوقع الموسمي.", icon=icon),
                ]
            chips: list[ft.Control] = [
                ft.Container(
                    ft.Text(
                        f"{MONTH_NAMES_AR.get(cal, cal)} {float(data['seasonal'].get(cal, 1.0)):.2f}",
                        size=9.5, weight=ft.FontWeight.BOLD,
                        color=Colors.PRIMARY if float(data["seasonal"].get(cal, 1.0)) > 1.05 else Colors.TEXT_SECONDARY,
                    ),
                    padding=ft.padding.symmetric(horizontal=8, vertical=5),
                    bgcolor=Colors.PRIMARY_BG if float(data["seasonal"].get(cal, 1.0)) > 1.05 else Colors.BACKGROUND_ALT,
                    border_radius=Radius.MD,
                )
                for cal in range(1, 13)
            ]
            entries: list[ft.Control] = []
            for f in data["forecasts"]:
                entries.append(
                    self._entry(
                        icon=icon,
                        accent=accent,
                        title=f"{f['month_label']} {f['month']}",
                        subtitle=f"المؤشر الموسمي {f['seasonal_index']:.2f}",
                        value=self.money(f["forecast"]),
                        value_sub=f"{self.money(f['low'])} – {self.money(f['high'])}",
                    )
                )
            return [
                self._section_title(icon, title),
                metric_row([
                    self._metric("المتوسط الشهري", self.money(data["monthly_average"]), icon=ft.Icons.CALCULATE_OUTLINED, accent=accent),
                    self._metric("أساس الاتجاه", self.money(data["trend_base"]), icon=ft.Icons.ROUTE_OUTLINED, accent=Colors.ORANGE),
                    self._metric("كفاية البيانات", data["confidence_label"], icon=ft.Icons.VERIFIED_OUTLINED, accent=Colors.SUCCESS, note=f"نطاق التذبذب ±{data['band_percent']}٪"),
                    self._metric("أشهر البيانات", str(data["history_months"]), icon=ft.Icons.DATE_RANGE_OUTLINED, accent=Colors.PURPLE),
                ], spacing=10),
                ft.Container(
                    ft.Column(
                        [
                            ft.Text("المؤشر الموسمي حسب الشهر التقويمي (أعلى من 1.05 = ذروة موسمية)", size=10.5, color=Colors.TEXT_SECONDARY),
                            ft.Row(chips, spacing=6, wrap=True),
                        ],
                        spacing=8,
                    ),
                    padding=12,
                    border=ft.border.all(1, Colors.BORDER_ALT),
                    border_radius=Radius.LG,
                    bgcolor=Colors.WHITE,
                    shadow=Shadow.SM,
                ),
                *entries,
            ]

        blocks: list[ft.Control] = [
            self._footnote(
                "التوقع = متوسط آخر أشهر الفترة × المؤشر الموسمي للشهر القادم، مع نطاق ± يعتمد على تقلب البيانات. يُحسب من فواتير البيع/الشراء في قاعدة البيانات."
            ),
        ]
        if accuracy["insufficient"]:
            blocks.append(self._footnote(accuracy["reason"]))
        else:
            blocks.extend([
                self._section_title(ft.Icons.FACT_CHECK_OUTLINED, "اختبار رجعي لدقة التوقع"),
                self._footnote(
                    f"متوسط آخر 6 أشهر اختُبر على {accuracy['sample_months']} أشهر محفوظة خارج العينة؛ "
                    f"متوسط الخطأ المطلق {self.money(accuracy['mae'])}، وWAPE {accuracy['wape_percent']:.1f}%."
                ),
            ])
        blocks += direction_card("توقعات المبيعات", ft.Icons.TRENDING_UP_ROUNDED, Colors.PRIMARY, sales)
        blocks += direction_card("توقعات المشتريات", ft.Icons.TRENDING_DOWN_ROUNDED, Colors.ORANGE, purchases)
        return blocks


__all__ = ["ReportsCenter"]
