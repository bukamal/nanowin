from __future__ import annotations

import csv as _csv
import tempfile
from datetime import datetime
from pathlib import Path

import flet as ft

from nano_offline.core.toast import toast
from nano_offline.core.party_reliability import grade_party

from nano_offline.components import SelectAllTextField, empty_state, new_form_sheet, render_form_sheet, metric_row, metric_controls, DataGrid, GridColumn
from nano_offline.components.column_visibility import ColumnVisibility
from nano_offline.components.print_profile import apply_print_profile, open_print_profile_dialog
from nano_offline.core.theme import Colors, Shadow
from nano_offline.core import privacy


# Desktop grid columns for the customers/suppliers screens. Balance is the
# headline number; grade summarizes outstanding-invoice behavior.
PARTY_GRID_COLUMNS = [
    GridColumn("name", "الاسم", tooltip="اسم العميل أو المورد — اضغط للترتيب"),
    GridColumn("phone", "الهاتف", width=120, align="center", tooltip="رقم الهاتف المسجّل — يُخفى تلقائيًا على الشاشات الضيقة"),
    GridColumn("balance", "الرصيد", width=120, align="end", kind="money", tooltip="الرصيد الحالي: موجب يعني مبلغًا مستحقًا على الطرف"),
    GridColumn("_grade", "التقييم", width=170, align="center", kind="badge", sortable=False, tooltip="تقييم انتظام السداد بناءً على الفواتير غير المسددة"),
]


class PartyCenter:
    """Shared customer/supplier list, search, detail dialog, add/edit/delete.

    A single instance is reused for both customers and suppliers; the
    repository and title are passed to :meth:`show_center` per navigation.
    Extracted from ``main.py`` (previously the inline ``party_view`` closure).
    """

    def __init__(self, page: ft.Page, ctx, content: ft.Container, *, native_files=None, on_title_change=None, on_open_receipt=None, on_open_payment=None, on_open_statement=None):
        self.page = page
        self.ctx = ctx
        self.content = content
        self.native_files = native_files
        self.on_title_change = on_title_change
        self.on_open_receipt = on_open_receipt
        self.on_open_payment = on_open_payment
        self.on_open_statement = on_open_statement

    def _set_header(self, title: str, subtitle: str = "") -> None:
        if self.on_title_change:
            self.on_title_change(title, subtitle)

    def money(self, value) -> str:
        return privacy.money_ctx(value, self.ctx)

    def notify(self, text: str, kind: str | None = None, sound_kind: str | None = None) -> None:
        toast(self.page, text, kind=kind, sound_kind=sound_kind)

    def show_center(self, repo, title: str) -> None:
        page = self.page
        content = self.content
        notify = self.notify
        money = self.money
        is_customer = repo.table == "customers"
        singular = "العميل" if is_customer else "المورد"
        self._set_header(title, f"إدارة بيانات {title} والحسابات المرتبطة")
        search = SelectAllTextField(
            label=f"بحث في {title}",
            hint_text="الاسم أو الهاتف",
            prefix_icon=ft.Icons.SEARCH,
            expand=True,
            on_focus=lambda e: grid.disarm(),
        )
        # Same paging as the items/invoices screens: build only a page of
        # rows at a time instead of every matching customer/supplier.
        render_limit = {"n": 60}
        summary_row = ft.ResponsiveRow(spacing=8, run_spacing=8)

        # ---- desktop grid (same spreadsheet treatment as items/invoices) --
        grid_sort = {"key": None, "dir": None}
        density_state = {"compact": self.ctx.settings.get("ui.parties_density", "comfortable") == "compact"}
        selected_state = {"rows": []}
        outstanding_state = {"map": {}}
        export_state = {"rows": []}
        columns_visibility = ColumnVisibility(
            columns=PARTY_GRID_COLUMNS,
            settings=self.ctx.settings,
            preference_key="ui.parties_hidden_columns",
            auto_hidden={"phone"} if (page.width or 1200) < 720 else set(),
            on_change=lambda: (
                grid.set_hidden_keys(columns_visibility.hidden_keys),
                page.update(),
            ),
        )
        columns_button = columns_visibility.button()

        async def export_csv(_=None) -> None:
            rows_out = export_state["rows"]
            if not rows_out:
                notify("لا توجد بيانات للتصدير", kind="warning")
                return
            if self.native_files is None:
                notify("تصدير الملفات غير مهيأ في هذا البناء", kind="error")
                return
            try:
                path = Path(tempfile.gettempdir()) / f"nano_parties_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = _csv.writer(fh)
                    writer.writerow(["الاسم", "الهاتف", "العنوان", "الرصيد", "التقييم"])
                    for party in rows_out:
                        rel = party_grade(party)
                        writer.writerow([
                            party.get("name") or "",
                            party.get("phone") or "",
                            party.get("address") or "",
                            party.get("balance") or 0,
                            (rel or {}).get("label") or "",
                        ])
                await self.native_files.share_file(str(path), mime_type="text/csv", subject=f"تصدير {title}")
                notify("تم تجهيز ملف CSV (بترميز يفتح في Excel العربي)")
            except Exception as exc:
                notify(str(exc), kind="error")

        export_button = ft.IconButton(
            icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
            tooltip="تصدير CSV للعرض الحالي",
            on_click=export_csv,
        )

        def party_grade(row: dict) -> dict | None:
            try:
                return grade_party(
                    balance=float(row.get("balance") or 0),
                    outstanding_rows=outstanding_state["map"].get(int(row.get("id") or 0), []),
                )
            except Exception:
                return None

        def _grid_formatter(col, value, row):
            if col.key == "balance":
                return money(value)
            if col.key == "phone":
                return str(value or "—")
            if col.key == "_grade":
                rel = party_grade(row)
                if not rel:
                    return ("—", Colors.TEXT_SECONDARY, Colors.BACKGROUND)
                label = rel["label"]
                if rel.get("max_age_days"):
                    label = f"{label} · {rel['max_age_days']}ي"
                return (label, getattr(Colors, rel["color_token"], Colors.PRIMARY), getattr(Colors, rel["bg_token"], Colors.PRIMARY_BG))
            return None

        def party_row_bg(row: dict) -> str | None:
            rel = party_grade(row)
            if rel and rel.get("color_token") == "danger":
                return Colors.DANGER_BG
            return None

        def party_expand_panel(row: dict) -> ft.Control:
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
                    chip(ft.Icons.PHONE_OUTLINED, "الهاتف", str(row.get("phone") or "بلا هاتف")),
                    chip(ft.Icons.LOCATION_ON_OUTLINED, "العنوان", str(row.get("address") or "بلا عنوان")),
                    chip(ft.Icons.CALENDAR_MONTH_OUTLINED, "أُضيف", str(row.get("created_at") or "—")[:10]),
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

        def small_metric(label: str, value: str, icon, accent: str):
            return ft.Container(
                ft.Row(
                    [
                        ft.Container(ft.Icon(icon, size=19, color=accent), width=38, height=38, alignment=ft.alignment.center, bgcolor=Colors.BACKGROUND, border_radius=12),
                        ft.Column([ft.Text(label, size=10, color=Colors.TEXT_SECONDARY), ft.Text(value, size=17, weight=ft.FontWeight.BOLD)], spacing=1, expand=True),
                    ]
                ),
                padding=11,
                bgcolor=Colors.WHITE,
                border=ft.border.all(1, Colors.BORDER),
                border_radius=16,
                shadow=Shadow.SM,
            )

        def open_editor(party: dict | None = None):
            name = SelectAllTextField(label="الاسم", value=(party or {}).get("name", ""))
            phone = SelectAllTextField(label="الهاتف", value=(party or {}).get("phone") or "", keyboard_type=ft.KeyboardType.PHONE)
            address = SelectAllTextField(label="العنوان", value=(party or {}).get("address") or "", multiline=True, min_lines=1, max_lines=3)
            sheet = new_form_sheet()

            def close(_=None):
                page.close(sheet)

            def save(_=None):
                try:
                    if party:
                        repo.update(int(party["id"]), name.value or "", phone.value, address.value)
                        message = f"تم تحديث {singular}"
                    else:
                        repo.create(name.value or "", phone.value, address.value)
                        message = f"تمت إضافة {singular}"
                    close()
                    notify(message)
                    refresh()
                except Exception as exc:
                    notify(str(exc), kind="error")

            render_form_sheet(
                page, sheet,
                title=f"{'تعديل' if party else 'إضافة'} {singular}",
                fields=[name, phone, address],
                on_close=close, on_save=save,
            )
            page.open(sheet)

        def confirm_delete(party: dict, parent_dialog=None, restore_on_cancel: bool = False):
            # Kept parameter-compatible with the old swipe-to-delete rows
            # (SWIPE_ACTIONS_NOTES.md): the desktop grid now routes deletes
            # through the ⋮ row menu and the bulk bar into this same guarded
            # dialog, which re-reads from `repo` after cancelling or
            # deleting so the list always reflects actual data.
            confirm = ft.AlertDialog(modal=True)

            def close(_=None):
                page.close(confirm)
                if restore_on_cancel:
                    refresh()

            def remove(_=None):
                try:
                    repo.delete(int(party["id"]))
                    close()
                    if parent_dialog:
                        try:
                            page.close(parent_dialog)
                        except Exception:
                            pass
                    notify(f"تم حذف {singular}", kind="success", sound_kind="delete")
                    refresh()
                except Exception as exc:
                    close()
                    notify(str(exc), kind="error")

            confirm.title = ft.Text(f"حذف {singular}")
            confirm.content = ft.Text(f"هل تريد حذف «{party['name']}»؟ لن يسمح بالحذف إذا كانت هناك حركات مالية مرتبطة.")
            confirm.actions = [ft.TextButton("إلغاء", on_click=close), ft.FilledButton("حذف", icon=ft.Icons.DELETE_FOREVER, on_click=remove)]
            page.open(confirm)

        def show_detail(party: dict):
            try:
                data = repo.activity_summary(int(party["id"]))
            except Exception as exc:
                notify(str(exc), kind="error")
                return
            balance = float(data.get("balance") or 0)
            party_type = "customer" if title == "العملاء" else "supplier"
            try:
                open_rows = [
                    r for r in self.ctx.reports.outstanding_invoices(party_type)
                    if int(r.get("party_id") or 0) == int(party["id"])
                ]
            except Exception:
                open_rows = []
            try:
                rel = grade_party(balance=balance, outstanding_rows=open_rows)
            except Exception as exc:
                notify(str(exc), kind="error")
                return
            grade_color = getattr(Colors, rel["color_token"], Colors.PRIMARY)
            grade_bg = getattr(Colors, rel["bg_token"], Colors.PRIMARY_BG)
            # Numeric trust score (0–100) — richer offline signal
            trust = None
            try:
                trust = self.ctx.party_trust.score(
                    int(party["id"]),
                    party_type="customer" if is_customer else "supplier",
                )
            except Exception:
                trust = None
            recent = []
            for inv in data.get("recent_invoices") or []:
                remaining = float(inv.get("remaining_amount") or 0)
                recent.append(
                    ft.Container(
                        ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(f"فاتورة #{inv['id']}", size=12, weight=ft.FontWeight.BOLD),
                                        ft.Text(str(inv.get("invoice_date") or "—"), size=10, color=Colors.TEXT_SECONDARY),
                                    ], spacing=1, expand=True,
                                ),
                                ft.Column(
                                    [
                                        ft.Text(money(inv.get("total")), size=12, weight=ft.FontWeight.BOLD),
                                        ft.Text("مسددة" if remaining <= 1e-9 else f"متبقي {money(remaining)}", size=9, color=Colors.SUCCESS if remaining <= 1e-9 else Colors.ORANGE),
                                    ], spacing=1, horizontal_alignment=ft.CrossAxisAlignment.END,
                                ),
                            ]
                        ),
                        padding=9, bgcolor=Colors.BACKGROUND, border_radius=12,
                    )
                )
            if not recent:
                recent = [ft.Text("لا توجد فواتير مرتبطة بعد", size=11, color=Colors.TEXT_SECONDARY)]

            # Bottom sheet instead of a centered AlertDialog -- matches the
            # same BottomSheet + drag-handle + rounded-top treatment already
            # used for the item detail view in items_view.py, so customer/
            # supplier detail reads consistently with the rest of the app.
            detail_sheet = ft.BottomSheet(content=ft.Container(), enable_drag=True, maintain_bottom_view_insets_padding=True)

            def close(_=None):
                page.close(detail_sheet)

            def edit(_=None):
                close()
                open_editor(data)

            party_type = "customer" if is_customer else "supplier"

            def party_card_document_html() -> str:
                return self.ctx.documents.party_card_html(party_type, data)

            async def print_party_card(_=None):
                if self.native_files is None:
                    notify("الطباعة الأصلية غير مهيأة في هذا البناء")
                    return

                async def print_selected(layout: str, width: int) -> None:
                    try:
                        html = apply_print_profile(party_card_document_html(), layout, width)
                        await self.native_files.print_html(
                            html,
                            name=f"nano-party-{data['id']}",
                            paper_layout=layout,
                            roll_width_mm=width,
                        )
                    except Exception as exc:
                        notify(str(exc), kind="error")

                open_print_profile_dialog(
                    page,
                    self.ctx.settings,
                    print_selected,
                    surface="party",
                    native_files=self.native_files,
                    preview_builder=lambda _layout, _width: party_card_document_html(),
                )

            async def share_party_card_pdf(_=None):
                if self.native_files is None:
                    notify("تصدير PDF غير مهيأ في هذا البناء")
                    return

                async def share_selected(layout: str, width: int) -> None:
                    try:
                        html = apply_print_profile(party_card_document_html(), layout, width)
                        await self.native_files.share_pdf(
                            html,
                            filename=f"nano_party_{data['id']}.pdf",
                            paper_layout=layout,
                            roll_width_mm=width,
                        )
                    except Exception as exc:
                        notify(str(exc), kind="error")

                open_print_profile_dialog(
                    page,
                    self.ctx.settings,
                    share_selected,
                    title="تنسيق ملف PDF",
                    surface="party",
                    native_files=self.native_files,
                    preview_builder=lambda _layout, _width: party_card_document_html(),
                )

            # Same explicit-height approach as the item detail sheet: an
            # outer Container height computed from the real screen height,
            # with a scrollable stats/history body and a fixed footer row
            # for حذف/تعديل/إغلاق so the actions stay reachable regardless
            # of how many recent invoices are listed above them.
            page_h = page.height or 780
            total_sheet_h = min(int(page_h * 0.55), 600)
            header_reserved, footer_reserved, gaps = 90, 60, 24
            body_area_height = max(180, total_sheet_h - header_reserved - footer_reserved - gaps)

            def _detail_actions():
                actions = [
                    ft.TextButton("حذف", icon=ft.Icons.DELETE_OUTLINE, on_click=lambda _: confirm_delete(data, detail_sheet), expand=True),
                    ft.OutlinedButton("تعديل", icon=ft.Icons.EDIT_OUTLINED, on_click=edit, expand=True),
                ]
                bal = abs(float(data.get("balance") or 0))
                if title == "العملاء" and self.on_open_receipt and bal > 1e-9:
                    def _receipt(_=None):
                        close()
                        self.on_open_receipt(
                            customer_id=int(data["id"]),
                            amount=max(0.0, float(data.get("balance") or 0)),
                        )
                    actions.append(
                        ft.FilledButton("سند قبض", icon=ft.Icons.PAYMENTS_OUTLINED, on_click=_receipt, expand=True)
                    )
                elif title == "الموردون" and self.on_open_payment and bal > 1e-9:
                    def _pay(_=None):
                        close()
                        self.on_open_payment(
                            supplier_id=int(data["id"]),
                            amount=max(0.0, float(data.get("balance") or 0)),
                        )
                    actions.append(
                        ft.FilledButton("سند صرف", icon=ft.Icons.PAYMENTS_OUTLINED, on_click=_pay, expand=True)
                    )
                if self.on_open_statement is not None:
                    def _statement(_=None):
                        close()
                        self.on_open_statement(party_type, int(data["id"]))
                    actions.append(
                        ft.OutlinedButton("كشف الحساب", icon=ft.Icons.DESCRIPTION_OUTLINED, on_click=_statement, expand=True)
                    )
                actions.append(ft.FilledButton("إغلاق", on_click=close, expand=True))
                return actions

            detail_sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Row(
                            [
                                ft.Container(ft.Icon(ft.Icons.PERSON if is_customer else ft.Icons.LOCAL_SHIPPING_OUTLINED, color=Colors.PRIMARY), width=42, height=42, alignment=ft.alignment.center, bgcolor=Colors.PRIMARY_BG, border_radius=14),
                                ft.Column([
                                    ft.Text(data["name"], size=18, weight=ft.FontWeight.BOLD),
                                    ft.Row([
                                        ft.Text(data.get("phone") or "بدون هاتف", size=11, color=Colors.TEXT_SECONDARY),
                                        ft.Container(
                                            ft.Text(rel["label"], size=10, weight=ft.FontWeight.W_600, color=grade_color),
                                            padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                            bgcolor=grade_bg,
                                            border_radius=10,
                                        ),
                                    ], spacing=8),
                                ], spacing=1, expand=True),
                                ft.IconButton(ft.Icons.PRINT_OUTLINED, tooltip="طباعة بطاقة الحساب", on_click=print_party_card),
                                ft.IconButton(ft.Icons.IOS_SHARE, tooltip="مشاركة PDF", on_click=share_party_card_pdf),
                            ]
                        ),
                        ft.Container(
                            ft.Column(
                                [
                                    metric_row([
                                        small_metric("الرصيد الحالي", money(balance), ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.PRIMARY),
                                        small_metric("عدد الفواتير", str(int(data.get("invoice_count") or 0)), ft.Icons.RECEIPT_LONG_OUTLINED, Colors.PURPLE),
                                        small_metric("إجمالي الفواتير", money(data.get("invoice_total")), ft.Icons.PAID_OUTLINED, Colors.SUCCESS),
                                        small_metric("انتظام السداد", rel["label"] + (f" · {rel['max_age_days']}ي" if rel["max_age_days"] else ""), ft.Icons.VERIFIED_USER_OUTLINED, grade_color),
                                        small_metric("درجة الثقة", f"{trust.score}/100 · {trust.label}" if trust else "—", ft.Icons.SHIELD_OUTLINED, getattr(Colors, {"success":"SUCCESS","info":"PRIMARY","warning":"WARNING","danger":"DANGER","muted":"TEXT_MUTED"}.get(trust.color_key if trust else "muted", "PRIMARY"), Colors.PRIMARY)),
                                    ], spacing=7),
                                    ft.Text(f"العنوان: {data.get('address') or '—'}", size=11, color=Colors.TEXT_MUTED),
                                    ft.Divider(height=10),
                                    ft.Text("آخر الفواتير", size=14, weight=ft.FontWeight.BOLD),
                                    ft.Column(recent, spacing=6),
                                ], spacing=9, scroll=ft.ScrollMode.AUTO,
                            ),
                            height=body_area_height, padding=ft.padding.only(top=8),
                        ),
                        ft.Container(
                            ft.Row(
                                _detail_actions(),
                                spacing=10,
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

        def _outstanding_by_party() -> dict[int, list[dict]]:
            party_type = "customer" if "customer" in str(getattr(repo, "table", "") or getattr(repo, "party_type", "") or title).lower() or title == "العملاء" else "supplier"
            if title == "العملاء":
                party_type = "customer"
            elif title == "الموردون":
                party_type = "supplier"
            try:
                rows = self.ctx.reports.outstanding_invoices(party_type)
            except Exception:
                return {}
            by: dict[int, list[dict]] = {}
            for r in rows:
                pid = r.get("party_id")
                if pid is None:
                    continue
                try:
                    pid = int(pid)
                except Exception:
                    continue
                by.setdefault(pid, []).append(r)
            return by

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
            dialog = ft.AlertDialog(modal=True, title=ft.Text(f"تأكيد حذف {title}"))

            def yes(_ev=None) -> None:
                page.close(dialog)
                deleted = 0
                failures = 0
                for party in chosen:
                    try:
                        repo.delete(int(party.get("id") or 0))
                        deleted += 1
                    except Exception:
                        failures += 1
                grid.clear_selection()
                refresh()
                if failures:
                    notify(f"حُذف {deleted}، وتعذّر حذف {failures} لارتباطها بحركات مالية", kind="warning")
                else:
                    notify(f"تم حذف {deleted}", sound_kind="success")

            def no(_ev=None) -> None:
                page.close(dialog)

            dialog.content = ft.Text(f"سيتم حذف {len(chosen)} من {title}. من لديه حركات مالية مرتبطة لن يُحذف.")
            dialog.actions = [
                ft.TextButton("إلغاء", on_click=no),
                ft.FilledButton("حذف", on_click=yes, style=ft.ButtonStyle(bgcolor=Colors.DANGER, color=Colors.WHITE)),
            ]
            page.open(dialog)

        def toggle_density(_=None) -> None:
            compact = not density_state["compact"]
            density_state["compact"] = compact
            grid.set_row_height(36 if compact else 48)
            density_button.icon = ft.Icons.DENSITY_SMALL if compact else ft.Icons.DENSITY_MEDIUM
            density_button.tooltip = "عرض مريح" if compact else "عرض مضغوط"
            self.ctx.settings.set("ui.parties_density", "compact" if compact else "comfortable")
            page.update()

        density_button = ft.IconButton(
            icon=ft.Icons.DENSITY_SMALL if density_state["compact"] else ft.Icons.DENSITY_MEDIUM,
            tooltip="عرض مريح" if density_state["compact"] else "عرض مضغوط",
            on_click=toggle_density,
        )
        bulk_bar = ft.Container(visible=False)

        def party_menu(row: dict) -> list:
            items: list = [
                ("فتح الحساب", ft.Icons.OPEN_IN_NEW_ROUNDED, lambda r=row: show_detail(dict(r))),
                ("تعديل", ft.Icons.EDIT_OUTLINED, lambda r=row: open_editor(dict(r))),
            ]
            balance = abs(float(row.get("balance") or 0))
            if is_customer and self.on_open_receipt and balance > 1e-9:
                items.append((
                    "سند قبض", ft.Icons.PAYMENTS_OUTLINED,
                    lambda r=row: self.on_open_receipt(customer_id=int(r["id"]), amount=max(0.0, float(r.get("balance") or 0))),
                ))
            if (not is_customer) and self.on_open_payment and balance > 1e-9:
                items.append((
                    "سند صرف", ft.Icons.PAYMENTS_OUTLINED,
                    lambda r=row: self.on_open_payment(supplier_id=int(r["id"]), amount=max(0.0, float(r.get("balance") or 0))),
                ))
            if self.on_open_statement is not None:
                items.append((
                    "كشف الحساب", ft.Icons.DESCRIPTION_OUTLINED,
                    lambda r=row: self.on_open_statement("customer" if is_customer else "supplier", int(r["id"])),
                ))
            items.append(None)
            items.append(("حذف", ft.Icons.DELETE_OUTLINE, lambda r=row: confirm_delete(dict(r))))
            return items

        grid_height = max(300, min(560, int((page.height or 760) * 0.52)))
        grid = DataGrid(
            columns=PARTY_GRID_COLUMNS,
            rows=[],
            page=page,
            row_height=36 if density_state["compact"] else 48,
            viewport_height=grid_height,
            hidden_keys=columns_visibility.hidden_keys,
            on_row_activate=lambda row: show_detail(dict(row)),
            on_sort=apply_grid_sort,
            selectable=True,
            on_selection_change=render_bulk_bar,
            on_row_menu=party_menu,
            row_bg=party_row_bg,
            on_row_expand=party_expand_panel,
            on_search_shortcut=focus_search,
            on_new_shortcut=lambda: open_editor(),
            formatter=_grid_formatter,
            boxed=True,
            center_cells=True,
            activate_tooltip="فتح الحساب",
            empty_text="لا توجد نتائج",
        )
        grid_more = ft.Container(visible=False)
        desktop_list = ft.Column([bulk_bar, ft.Container(grid), grid_more], spacing=8)
        list_switch = ft.Container(content=desktop_list)

        def refresh(_=None):
            parties = repo.list(search.value or "")
            all_parties = repo.list()
            outstanding_map = _outstanding_by_party()
            total_balance = sum(float(x.get("balance") or 0) for x in all_parties)
            positive = sum(1 for x in all_parties if abs(float(x.get("balance") or 0)) > 1e-9)
            summary_row.controls = metric_controls([
                small_metric(f"عدد {title}", str(len(all_parties)), ft.Icons.GROUP_OUTLINED, Colors.PRIMARY),
                small_metric("إجمالي الأرصدة", money(total_balance), ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.SUCCESS if total_balance >= 0 else Colors.DANGER),
                small_metric("حسابات بحركة", str(positive), ft.Icons.SYNC_ALT, Colors.PURPLE),
            ])
            outstanding_state["map"] = outstanding_map
            if grid_sort["key"]:
                sort_key = grid_sort["key"]
                reverse = grid_sort["dir"] == "desc"

                def sort_value(party):
                    if sort_key == "balance":
                        try:
                            return float(party.get("balance") or 0)
                        except (TypeError, ValueError):
                            return 0.0
                    return str(party.get(sort_key) or "")

                parties.sort(key=sort_value, reverse=reverse)
            export_state["rows"] = parties
            grid.set_hidden_keys(columns_visibility.hidden_keys)
            if not parties:
                grid.set_rows([])
                if all_parties:
                    list_switch.content = empty_state(
                        "لا توجد نتائج مطابقة للبحث",
                        icon=ft.Icons.PERSON_SEARCH,
                        hint="جرّب اسمًا أو رقم هاتف مختلف",
                    )
                else:
                    list_switch.content = empty_state(
                        f"لا يوجد {title} بعد",
                        icon=ft.Icons.PERSON_SEARCH,
                        hint=f"ابدأ بإضافة أول {singular}",
                        action_label=f"إضافة {singular}",
                        on_action=lambda _: open_editor(),
                    )
                page.update()
                return
            shown = parties[: render_limit["n"]]
            grid.set_rows(shown)
            remaining = len(parties) - len(shown)
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
            page.update()

        def refresh_from_search(e=None):
            render_limit["n"] = 60
            refresh()

        search.on_change = refresh_from_search
        summary_visible = {
            "value": self.ctx.settings.get("ui.parties_summary_visible", "true").strip().lower()
            in ("1", "true", "yes", "on")
        }
        summary_row.visible = summary_visible["value"]

        def toggle_summary(_=None):
            summary_visible["value"] = not summary_visible["value"]
            summary_row.visible = summary_visible["value"]
            self.ctx.settings.set("ui.parties_summary_visible", str(summary_visible["value"]).lower())
            summary_button.icon = ft.Icons.VISIBILITY_OFF_OUTLINED if summary_visible["value"] else ft.Icons.INSIGHTS_OUTLINED
            summary_button.tooltip = "إخفاء الملخص" if summary_visible["value"] else "إظهار الملخص"
            page.update()

        summary_button = ft.IconButton(
            icon=ft.Icons.VISIBILITY_OFF_OUTLINED if summary_visible["value"] else ft.Icons.INSIGHTS_OUTLINED,
            tooltip="إخفاء الملخص" if summary_visible["value"] else "إظهار الملخص",
            on_click=toggle_summary,
        )

        # Same sticky_top/scroll_body/bottom_bar treatment as the items and
        # stocktake screens: search stays reachable while scrolling a long
        # party list, and the primary "إضافة" action stays one tap away at
        # the bottom instead of scrolling off with the list.
        sticky_top = ft.Container(
            ft.Row(
                [
                    ft.Container(search, width=280),
                    ft.Container(expand=True),
                    summary_button,
                    columns_button,
                    export_button,
                    density_button,
                ],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.only(left=18, right=18, top=12, bottom=10),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
        )
        scroll_body = ft.Column([summary_row, list_switch], spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)
        bottom_bar = ft.Container(
            ft.FilledButton(f"إضافة {singular}", icon=ft.Icons.ADD, on_click=lambda _: open_editor(), expand=True),
            padding=ft.padding.only(left=18, right=18, top=10, bottom=10),
            bgcolor=Colors.WHITE,
            border=ft.border.only(top=ft.BorderSide(1, Colors.BORDER)),
            shadow=ft.BoxShadow(blur_radius=16, color=Colors.BORDER, offset=ft.Offset(0, -4)),
        )
        content.content = ft.Column(
            [
                sticky_top,
                ft.Container(scroll_body, padding=ft.padding.only(left=18, right=18, top=14, bottom=10), expand=True),
                bottom_bar,
            ],
            spacing=0,
            expand=True,
        )
        refresh()


__all__ = ["PartyCenter"]
