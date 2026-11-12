"""Graphical restaurant boards: tables + kitchen display (KDS).

Both are read-only views built purely on existing services (open sale orders,
section resolution, per-line printed_at) -- no new tables. The kitchen board
groups each open order's lines by section and colours each card by how long
ago it was sent, so the kitchen can see what is waiting and what is late.
"""

from __future__ import annotations

from datetime import datetime, timezone

import flet as ft

from nano_offline.components import sheets
from nano_offline.core.theme import Colors, Radius, Shadow

__all__ = ["open_tables_board", "open_kitchen_board"]


def _minutes_since(timestamp) -> float | None:
    """Minutes since a SQLite UTC ``CURRENT_TIMESTAMP`` value, or None."""
    if not timestamp:
        return None
    try:
        sent = datetime.strptime(str(timestamp)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return max(0.0, (datetime.now(timezone.utc) - sent).total_seconds() / 60.0)


def _age_color(minutes: float | None) -> str:
    if minutes is None:
        return Colors.TEXT_SECONDARY
    if minutes <= 10:
        return Colors.SUCCESS_DARK
    if minutes <= 20:
        return Colors.WARNING_DARKER
    return Colors.DANGER


def _age_label(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    return f"منذ {int(minutes)} د"


def _order_elapsed(order_lines: list[dict]) -> float | None:
    stamps = [line.get("printed_at") for line in order_lines if line.get("printed_at")]
    if not stamps:
        return None
    return _minutes_since(max(stamps))


def _order_ready(order_lines: list[dict]) -> bool:
    """True when the latest kitchen 'ready' is not older than the last send."""
    ready = [line.get("kitchen_ready_at") for line in order_lines if line.get("kitchen_ready_at")]
    if not ready:
        return False
    sent = [line.get("printed_at") for line in order_lines if line.get("printed_at")]
    if not sent:
        return True
    return max(ready) >= max(sent)


def _section_names(ctx) -> dict[int, str]:
    try:
        return {int(s["id"]): str(s["name"]) for s in ctx.sections.list()}
    except Exception:
        return {}


def open_tables_board(
    page: ft.Page, ctx, notify, money, on_open_order=None, on_free_table=None
) -> None:
    """A floor plan of tables grouped by area, coloured by status.

    Occupied tables (open orders whose reference matches a table name) show
    their total + age/ready badge and open the order on tap; free tables show
    grey and notify the host (``on_free_table``).
    """
    sheet, width = sheets.new_desktop_sheet(page, cap=1000)
    areas_row = ft.Row(spacing=14, run_spacing=14, wrap=True)

    def close(_=None) -> None:
        page.close(sheet)

    def _table_card(name: str, order: dict | None) -> ft.Control:
        if order is not None:
            try:
                invoice = ctx.invoices.get_invoice(int(order["id"])) or {}
            except Exception:
                invoice = {}
            elapsed = _order_elapsed(invoice.get("lines") or [])
            ready = _order_ready(invoice.get("lines") or [])
            color = Colors.SUCCESS_DARK if ready else _age_color(elapsed)

            def _open(_=None, i=int(order["id"])) -> None:
                if callable(on_open_order):
                    close()
                    on_open_order(i)
                else:
                    notify("افتح الطلب من شاشة الفواتير")

            return ft.Container(
                ft.Column(
                    [
                        ft.Text(name, size=14, weight=ft.FontWeight.BOLD),
                        ft.Text(money(float(order.get("remaining_amount") or 0)), size=13, weight=ft.FontWeight.W_600),
                        ft.Container(
                            ft.Text("جاهز" if ready else _age_label(elapsed), size=10, color="#FFFFFF"),
                            padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            bgcolor=color, border_radius=10,
                        ),
                    ],
                    spacing=3, tight=True,
                ),
                width=150, padding=12, bgcolor=Colors.WHITE,
                border=ft.border.all(2, color), border_radius=Radius.LG,
                shadow=Shadow.SM, ink=True, on_click=_open,
            )

        def _free(_=None, n=name) -> None:
            if callable(on_free_table):
                close()
                on_free_table(n)
            else:
                notify("طاولة متاحة — أنشئ الطلب من نقطة البيع")

        return ft.Container(
            ft.Column(
                [
                    ft.Text(name, size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                    ft.Text("متاحة", size=11, color=Colors.TEXT_SECONDARY),
                ],
                spacing=3, tight=True,
            ),
            width=150, padding=12, bgcolor=Colors.BACKGROUND_ALT,
            border=ft.border.all(1, Colors.BORDER), border_radius=Radius.LG,
            ink=True, on_click=_free,
        )

    def render() -> None:
        try:
            tables = ctx.tables.list(active_only=True)
        except Exception:
            tables = []
        try:
            orders = ctx.invoices.open_sale_orders(limit=80)
        except Exception:
            orders = []
        known = {str(t.get("name")) for t in tables}
        by_ref: dict[str, dict] = {}
        loose: list[dict] = []
        for order in orders:
            ref = str(order.get("reference") or "").strip()
            if ref and ref in known:
                by_ref[ref] = order
            else:
                loose.append(order)

        grouped: dict[str, list[dict]] = {}
        for table in tables:
            grouped.setdefault(str(table.get("area") or "بلا منطقة"), []).append(table)

        columns: list[ft.Control] = []
        for area, items in grouped.items():
            cards = [_table_card(str(t.get("name")), by_ref.get(str(t.get("name")))) for t in items]
            columns.append(ft.Container(
                ft.Column(
                    [
                        ft.Text(area, size=13, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK),
                        ft.Row(cards, spacing=10, run_spacing=10, wrap=True),
                    ],
                    spacing=8, tight=True,
                ),
                padding=12, bgcolor=Colors.BACKGROUND_ALT, border_radius=Radius.MD,
            ))

        if loose:
            loose_cards: list[ft.Control] = []
            for order in loose:
                label = str(order.get("reference") or "").strip() or f"#{order['id']}"
                loose_cards.append(_table_card(label, order))
            columns.append(ft.Container(
                ft.Column(
                    [
                        ft.Text("طلبات بلا طاولة", size=13, weight=ft.FontWeight.BOLD, color=Colors.WARNING_DARKER),
                        ft.Row(loose_cards, spacing=10, run_spacing=10, wrap=True),
                    ],
                    spacing=8, tight=True,
                ),
                padding=12, bgcolor=Colors.WARNING_BG, border_radius=Radius.MD,
            ))

        if not columns:
            columns.append(ft.Text(
                "لا توجد طاولات معرّفة — أضِفها من الإدارة ← المطعم ← الطاولات.",
                size=12, color=Colors.TEXT_SECONDARY,
            ))
        areas_row.controls = columns
        try:
            areas_row.update()
        except Exception:
            pass

    def new_order(_=None) -> None:
        if callable(on_open_order):
            close()
            on_open_order(None)
        else:
            notify("أنشئ الطلب من نقطة البيع")

    sheet.content = sheets.sheet_frame(
        ft.Column(
            [
                ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER),
                sheets.sheet_header(
                    title="لوحة الطاولات",
                    subtitle="خريطة الطاولات حسب المنطقة — اللون يشير للحالة والتأخير",
                    icon=ft.Icons.APPS_ROUNDED,
                    on_close=close,
                    trailing=ft.IconButton(icon=ft.Icons.REFRESH, tooltip="تحديث", on_click=lambda _=None: render()),
                ),
                ft.Row([ft.FilledButton("طلب جديد", icon=ft.Icons.ADD, on_click=new_order)]),
                ft.Container(
                    ft.Column([areas_row], scroll=ft.ScrollMode.AUTO),
                    expand=True,
                ),
            ],
            spacing=10, expand=True,
        ),
        width=width,
        height=sheets.sheet_height(page, 0.85),
    )
    render()
    page.open(sheet)


def open_kitchen_board(page: ft.Page, ctx, notify) -> None:
    """A kitchen display: each open order's lines grouped by section, with age."""
    sheet, width = sheets.new_desktop_sheet(page, cap=1100)
    board = ft.Row(spacing=10, run_spacing=10, wrap=True)

    def close(_=None) -> None:
        page.close(sheet)

    def render() -> None:
        try:
            orders = ctx.invoices.open_sale_orders(limit=60)
        except Exception:
            orders = []
        names = _section_names(ctx)
        cards: list[ft.Control] = []
        for order in orders:
            try:
                grouped = ctx.sections.invoice_section_lines(int(order["id"]))
            except Exception:
                grouped = {}
            try:
                invoice = ctx.invoices.get_invoice(int(order["id"])) or {}
            except Exception:
                invoice = {}
            elapsed = _order_elapsed(invoice.get("lines") or [])
            ready = _order_ready(invoice.get("lines") or [])
            label = str(order.get("reference") or "").strip() or f"#{order['id']}"
            body: list[ft.Control] = [
                ft.Row(
                    [
                        ft.Text(label, size=14, weight=ft.FontWeight.BOLD, expand=True),
                        ft.Container(
                            ft.Text("جاهز" if ready else _age_label(elapsed), size=10, color="#FFFFFF"),
                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                            bgcolor=Colors.SUCCESS_DARK if ready else _age_color(elapsed),
                            border_radius=10,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Divider(height=1),
            ]
            if not grouped:
                body.append(ft.Text("لا بنود مُسندة لأقسام", size=11, color=Colors.TEXT_SECONDARY))
            for section_id, lines in grouped.items():
                body.append(ft.Text(str(names.get(int(section_id)) or f"قسم {section_id}"),
                                    size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK))
                for line in lines:
                    body.append(ft.Text(
                        f"{float(line.get('quantity') or 0):g} × {line.get('name')}",
                        size=12, color=Colors.TEXT_PRIMARY,
                    ))
            if not ready:
                def _mark_ready(_=None, i=int(order["id"])) -> None:
                    try:
                        ctx.sections.mark_order_ready(int(i))
                    except Exception:
                        pass
                    render()
                body.append(ft.ElevatedButton("تم التحضير", icon=ft.Icons.CHECK_CIRCLE_OUTLINE, on_click=_mark_ready))
            cards.append(ft.Container(
                ft.Column(body, spacing=4, tight=True),
                width=240, padding=12,
                bgcolor=Colors.WHITE,
                border=ft.border.all(2, Colors.SUCCESS_DARK if ready else _age_color(elapsed)),
                border_radius=Radius.LG,
                shadow=Shadow.SM,
            ))
        if not cards:
            cards.append(ft.Text("لا توجد طلبات قيد التحضير.", size=12, color=Colors.TEXT_SECONDARY))
        board.controls = cards
        try:
            board.update()
        except Exception:
            pass

    sheet.content = sheets.sheet_frame(
        ft.Column(
            [
                ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER),
                sheets.sheet_header(
                    title="شاشة المطبخ",
                    subtitle="تذاكر التحضير حسب القسم — اللون يشير لزمن الانتظار",
                    icon=ft.Icons.RECEIPT_LONG_OUTLINED,
                    on_close=close,
                    trailing=ft.IconButton(icon=ft.Icons.REFRESH, tooltip="تحديث", on_click=lambda _=None: render()),
                ),
                ft.Container(
                    ft.Column([board], scroll=ft.ScrollMode.AUTO),
                    expand=True,
                ),
            ],
            spacing=10, expand=True,
        ),
        width=width,
        height=sheets.sheet_height(page, 0.85),
    )
    render()
    page.open(sheet)