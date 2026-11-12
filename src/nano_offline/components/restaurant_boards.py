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


def open_tables_board(page: ft.Page, ctx, notify, money, on_open_order=None) -> None:
    """A colour-coded grid of open orders (tables) with elapsed time."""
    sheet, width = sheets.new_desktop_sheet(page, cap=1000)
    grid = ft.Row(spacing=10, run_spacing=10, wrap=True)

    def close(_=None) -> None:
        page.close(sheet)

    def render() -> None:
        try:
            orders = ctx.invoices.open_sale_orders(limit=60)
        except Exception:
            orders = []
        cards: list[ft.Control] = []
        for order in orders:
            try:
                invoice = ctx.invoices.get_invoice(int(order["id"])) or {}
            except Exception:
                invoice = {}
            elapsed = _order_elapsed(invoice.get("lines") or [])
            ready = _order_ready(invoice.get("lines") or [])
            label = str(order.get("reference") or "").strip() or f"طاولة #{order['id']}"

            def _open(_=None, i=int(order["id"])) -> None:
                if callable(on_open_order):
                    close()
                    on_open_order(i)
                else:
                    notify("افتح الطلب من شاشة الفواتير")

            cards.append(ft.Container(
                ft.Column(
                    [
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
                        ft.Text(money(float(order.get("remaining_amount") or 0)), size=15, weight=ft.FontWeight.W_600),
                        ft.Text(f"#{order['id']}", size=10, color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=4, tight=True,
                ),
                width=200, padding=14,
                bgcolor=Colors.WHITE,
                border=ft.border.all(2, Colors.SUCCESS_DARK if ready else _age_color(elapsed)),
                border_radius=Radius.LG,
                shadow=Shadow.SM,
                ink=True, on_click=_open,
            ))
        if not cards:
            cards.append(ft.Text("لا توجد طلبات مفتوحة حاليًا.", size=12, color=Colors.TEXT_SECONDARY))
        grid.controls = cards
        try:
            grid.update()
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
                    subtitle="الطلبات المفتوحة مرتّبة حسب الزمن — اللون يشير للتأخير",
                    icon=ft.Icons.APPS_ROUNDED,
                    on_close=close,
                    trailing=ft.IconButton(icon=ft.Icons.REFRESH, tooltip="تحديث", on_click=lambda _=None: render()),
                ),
                ft.Row([ft.FilledButton("طلب جديد", icon=ft.Icons.ADD, on_click=new_order)]),
                ft.Container(
                    ft.Column([grid], scroll=ft.ScrollMode.AUTO),
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