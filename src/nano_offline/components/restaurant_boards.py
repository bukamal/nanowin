"""Restaurant workspaces: tables + kitchen (KDS) as reusable content.

Each workspace is a ``(control, render)`` pair so it can be embedded inline in
the POS "restaurant workspace" tabs and also wrapped in a bottom sheet (Admin).
Built on existing services only (open sale orders, section resolution,
per-line printed_at / kitchen_ready_at); no new tables.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import flet as ft

from nano_offline.components import sheets
from nano_offline.core.theme import Colors, Radius, Shadow

__all__ = [
    "tables_workspace",
    "kitchen_workspace",
    "open_tables_board",
    "open_kitchen_board",
]

#: Sheets re-render themselves on this cadence while open.
AUTO_REFRESH_SECONDS = 15


def _minutes_since(timestamp) -> float | None:
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
    """True only when every sent line has been marked ready after its send."""
    sent = [line for line in order_lines if line.get("printed_at")]
    if not sent:
        return False
    return all(
        line.get("kitchen_ready_at") and line["kitchen_ready_at"] >= line["printed_at"]
        for line in sent
    )


def _section_names(ctx) -> dict[int, str]:
    try:
        return {int(s["id"]): str(s["name"]) for s in ctx.sections.list()}
    except Exception:
        return {}


def _start_auto_refresh(page: ft.Page, render, stop: dict) -> None:
    async def _loop() -> None:
        while not stop.get("flag"):
            await asyncio.sleep(AUTO_REFRESH_SECONDS)
            if stop.get("flag"):
                break
            try:
                render()
            except Exception:
                pass

    try:
        page.run_task(_loop)
    except Exception:
        pass


def tables_workspace(
    ctx, notify, money, *, on_open_order=None, on_free_table=None, on_pay_order=None, use_tables: bool = True
) -> tuple[ft.Control, callable]:
    """Tables / open-orders workspace (grid or list) with status colours,
    filters and quick actions. Returns ``(control, render)``."""
    state = {"view": "grid", "filter": "all"}
    # NOTE: NOT wrap=True -- the toolbar ends with an expand spacer, and
    # Flutter's Wrap cannot host an Expanded child (it renders a blank grey
    # box that covers the whole board). A plain Row is correct here.
    toolbar = ft.Row(spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    areas_row = ft.Row(spacing=14, run_spacing=14, wrap=True)
    list_col = ft.Column(spacing=8, tight=True)
    # Stable holders created once. Re-parenting areas_row/list_col into a new
    # Column on every render (the previous code) left dangling parents and a
    # blank/grey area; swapping body.content between these fixed holders does
    # not re-parent anything.
    grid_holder = ft.Column([areas_row], scroll=ft.ScrollMode.AUTO, expand=True)
    list_holder = ft.Column([list_col], scroll=ft.ScrollMode.AUTO, expand=True)
    body = ft.Container(grid_holder, expand=True, bgcolor=Colors.WHITE)

    def _entry(name: str, order: dict | None) -> dict:
        if order is None:
            return {"name": name, "order": None, "key": "free", "label": "متاحة",
                    "color": Colors.TEXT_SECONDARY, "elapsed": None, "count": 0, "total": 0.0}
        try:
            invoice = ctx.invoices.get_invoice(int(order["id"])) or {}
        except Exception:
            invoice = {}
        order_lines = invoice.get("lines") or []
        elapsed = _order_elapsed(order_lines)
        ready = _order_ready(order_lines)
        if ready:
            key, label, color = "ready", "جاهزة", Colors.SUCCESS_DARK
        elif elapsed is None:
            key, label, color = "open", "مفتوحة", Colors.PRIMARY
        elif elapsed >= 20:
            key, label, color = "late", "متأخرة", Colors.DANGER
        else:
            key, label, color = "sent", "أُرسلت", Colors.WARNING_DARKER
        return {"name": name, "order": order, "key": key, "label": label, "color": color,
                "elapsed": elapsed, "count": len(order_lines),
                "total": float(order.get("remaining_amount") or 0)}

    def _open(order_id: int) -> None:
        if callable(on_open_order):
            on_open_order(int(order_id))
        else:
            notify("افتح الطلب من شاشة الفواتير")

    def _drag(table: dict, card: ft.Control) -> ft.Control:
        def _drop(e) -> None:
            try:
                src = getattr(getattr(e, "src", None), "data", None)
                if src is None:
                    return
                ctx.tables.reorder(int(src), int(table["id"]))
            except Exception as exc:
                notify(str(exc), kind="error")
                return
            render()

        return ft.DragTarget(
            group="nano-tables",
            content=ft.Draggable(group="nano-tables", content=card, data=str(table["id"])),
            on_accept=_drop,
        )

    def _order_card(entry: dict) -> ft.Control:
        order = entry["order"]
        color = entry["color"]

        def _pay(_=None, i=int(order["id"])) -> None:
            if callable(on_pay_order):
                on_pay_order(int(i))
            else:
                _open(int(i))

        return ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(entry["name"], size=14, weight=ft.FontWeight.BOLD, expand=True),
                            ft.Container(
                                ft.Text(entry["label"], size=9, color="#FFFFFF"),
                                padding=ft.padding.symmetric(horizontal=7, vertical=2),
                                bgcolor=color, border_radius=8,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(money(entry["total"]), size=16, weight=ft.FontWeight.W_600),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.RECEIPT_LONG_OUTLINED, size=13, color=Colors.TEXT_SECONDARY),
                            ft.Text(f"{entry['count']} بند", size=10, color=Colors.TEXT_SECONDARY),
                            ft.Container(expand=True),
                            ft.Text(_age_label(entry["elapsed"]), size=10, color=color),
                        ],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.TextButton("دفع", icon=ft.Icons.PAYMENTS_OUTLINED, on_click=_pay),
                            ft.TextButton("تعديل", icon=ft.Icons.EDIT_OUTLINED, on_click=lambda _=None, i=int(order["id"]): _open(i)),
                        ],
                        spacing=2,
                    ),
                ],
                spacing=4, tight=True,
            ),
            width=185, padding=12, bgcolor=Colors.WHITE,
            border=ft.border.all(2, color), border_radius=Radius.LG, shadow=Shadow.SM,
        )

    def _free_card(entry: dict) -> ft.Control:
        name = entry["name"]

        def _free(_=None, n=name) -> None:
            if callable(on_free_table):
                on_free_table(n)
            else:
                notify("طاولة متاحة — أنشئ الطلب من نقطة البيع")

        return ft.Container(
            ft.Column(
                [
                    ft.Text(name, size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                    ft.Text("متاحة", size=11, color=Colors.TEXT_SECONDARY),
                    ft.Container(height=6),
                    ft.OutlinedButton("فتح طلب", icon=ft.Icons.ADD, on_click=_free),
                ],
                spacing=3, tight=True,
            ),
            width=185, padding=12, bgcolor=Colors.BACKGROUND_ALT,
            border=ft.border.all(1, Colors.BORDER), border_radius=Radius.LG,
        )

    def _list_row(area, entry: dict) -> ft.Control:
        color = entry["color"]
        actions: list[ft.Control] = []
        if entry["order"] is not None:
            def _pay(_=None, i=int(entry["order"]["id"])) -> None:
                (on_pay_order or on_open_order)(int(i)) if (callable(on_pay_order) or callable(on_open_order)) else notify("افتح الطلب من شاشة الفواتير")
            actions = [
                ft.TextButton("دفع", icon=ft.Icons.PAYMENTS_OUTLINED, on_click=_pay),
                ft.TextButton("تعديل", icon=ft.Icons.EDIT_OUTLINED,
                              on_click=lambda _=None, i=int(entry["order"]["id"]): _open(i)),
            ]
        else:
            def _free(_=None, n=entry["name"]) -> None:
                on_free_table(n) if callable(on_free_table) else notify("طاولة متاحة")
            actions = [ft.OutlinedButton("فتح طلب", icon=ft.Icons.ADD, on_click=_free)]
        return ft.Container(
            ft.Row(
                [
                    ft.Container(width=6, height=34, bgcolor=color, border_radius=3),
                    ft.Column(
                        [
                            ft.Text(entry["name"], size=13, weight=ft.FontWeight.W_600),
                            ft.Text(f"{area or ''}", size=10, color=Colors.TEXT_SECONDARY) if area else ft.Container(height=0),
                        ],
                        spacing=0, width=150, tight=True,
                    ),
                    ft.Text(entry["label"], size=11, color=color, width=70),
                    ft.Text(f"{entry['count']} بند", size=11, color=Colors.TEXT_SECONDARY, width=70),
                    ft.Text(_age_label(entry["elapsed"]), size=11, color=Colors.TEXT_SECONDARY, width=70),
                    ft.Container(expand=True),
                    ft.Text(money(entry["total"]) if entry["order"] else "—", size=13, weight=ft.FontWeight.W_600),
                    *actions,
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=8, bgcolor=Colors.WHITE,
            border=ft.border.all(1, Colors.BORDER), border_radius=Radius.MD,
        )

    def _chip(key: str, label: str) -> ft.Control:
        active = state["filter"] == key
        return ft.Container(
            ft.Text(label, size=11, weight=ft.FontWeight.W_600, color=Colors.WHITE if active else Colors.TEXT_MUTED),
            padding=ft.padding.symmetric(horizontal=12, vertical=7),
            bgcolor=Colors.PRIMARY if active else Colors.WHITE,
            border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
            border_radius=16, ink=True,
            on_click=lambda _=None, k=key: _set_filter(k),
        )

    def _set_filter(key: str) -> None:
        state["filter"] = key
        render()

    def _set_view(mode: str) -> None:
        state["view"] = mode
        render()

    def render() -> None:
        try:
            orders = ctx.invoices.open_sale_orders(limit=80)
        except Exception:
            orders = []
        table_list = []
        if use_tables:
            try:
                table_list = ctx.tables.list(active_only=True)
            except Exception:
                table_list = []
        known = {str(t.get("name")) for t in table_list}
        table_by_name = {str(t.get("name")): t for t in table_list}
        by_ref: dict[str, dict] = {}
        loose: list[dict] = []
        for order in orders:
            ref = str(order.get("reference") or "").strip()
            if use_tables and ref and ref in known:
                by_ref[ref] = order
            else:
                loose.append(order)

        entries: list[tuple] = []  # (area, entry)
        if use_tables:
            grouped: dict[str, list[str]] = {}
            for table in table_list:
                grouped.setdefault(str(table.get("area") or "بلا منطقة"), []).append(str(table.get("name")))
            for area, names in grouped.items():
                for name in names:
                    entries.append((area, _entry(name, by_ref.get(name))))
        for order in loose:
            name = str(order.get("reference") or "").strip() or f"طلب #{order['id']}"
            entries.append((None, _entry(name, order)))

        fkey = state["filter"]
        if fkey != "all":
            entries = [item for item in entries if item[1]["key"] == fkey]

        if state["view"] == "list":
            if entries:
                list_col.controls = [_list_row(area, entry) for area, entry in entries]
            else:
                list_col.controls = [ft.Text("لا توجد نتائج.", size=12, color=Colors.TEXT_SECONDARY)]
            body.content = list_holder
        else:
            by_area: dict[str, list[dict]] = {}
            for area, entry in entries:
                by_area.setdefault(str(area or "الطلبات"), []).append(entry)
            columns: list[ft.Control] = []
            for area, area_entries in by_area.items():
                cards = []
                for e in area_entries:
                    card = _order_card(e) if e["order"] is not None else _free_card(e)
                    table = table_by_name.get(e["name"])
                    if table is not None:
                        card = _drag(table, card)
                    cards.append(card)
                columns.append(ft.Container(
                    ft.Column(
                        [ft.Text(area, size=12, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK),
                         ft.Row(cards, spacing=10, run_spacing=10, wrap=True)],
                        spacing=8, tight=True,
                    ),
                    padding=10, bgcolor=Colors.WHITE,
                    border=ft.border.all(1, Colors.BORDER_ALT), border_radius=Radius.MD,
                ))
            if not columns:
                columns.append(ft.Text("لا توجد نتائج.", size=12, color=Colors.TEXT_SECONDARY))
            areas_row.controls = columns
            body.content = grid_holder

        # Toolbar (filters + view toggle) rebuilt each render for active styles.
        view_grid_active = state["view"] == "grid"
        toolbar.controls = [
            _chip("all", "الكل"),
            _chip("open", "مفتوحة"),
            _chip("sent", "أُرسلت"),
            _chip("ready", "جاهزة"),
            _chip("late", "متأخرة"),
            *([_chip("free", "متاحة")] if use_tables else []),
            ft.Container(expand=True),
            ft.IconButton(
                icon=ft.Icons.GRID_VIEW, tooltip="شبكة",
                icon_color=Colors.PRIMARY if view_grid_active else Colors.TEXT_SECONDARY,
                on_click=lambda _=None: _set_view("grid"), icon_size=18,
            ),
            ft.IconButton(
                icon=ft.Icons.VIEW_LIST, tooltip="قائمة",
                icon_color=Colors.TEXT_SECONDARY if view_grid_active else Colors.PRIMARY,
                on_click=lambda _=None: _set_view("list"), icon_size=18,
            ),
        ]
        try:
            toolbar.update()
        except Exception:
            pass
        try:
            body.update()
        except Exception:
            pass

    control = ft.Container(
        ft.Column([toolbar, body], spacing=8, expand=True),
        expand=True, padding=4, bgcolor=Colors.WHITE,
    )
    return control, render


def kitchen_workspace(ctx, notify) -> tuple[ft.Control, callable]:
    """Kitchen display (KDS) as a control + its render function."""
    board = ft.Row(spacing=10, run_spacing=10, wrap=True)

    def render() -> None:
        try:
            orders = ctx.invoices.open_sale_orders(limit=80)
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
            sent_lines = [ln for ln in (invoice.get("lines") or []) if ln.get("printed_at")]
            ready_lines = [
                ln for ln in sent_lines
                if ln.get("kitchen_ready_at") and ln["kitchen_ready_at"] >= ln["printed_at"]
            ]
            status_text = "جاهز" if ready else _age_label(elapsed)
            if sent_lines and not ready:
                status_text = f"{_age_label(elapsed)} · {len(ready_lines)}/{len(sent_lines)}"
            body: list[ft.Control] = [
                ft.Row(
                    [
                        ft.Text(label, size=14, weight=ft.FontWeight.BOLD, expand=True),
                        ft.Container(
                            ft.Text(status_text, size=10, color="#FFFFFF"),
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
                    line_ready = (
                        line.get("printed_at") and line.get("kitchen_ready_at")
                        and line["kitchen_ready_at"] >= line["printed_at"]
                    )
                    row_controls: list[ft.Control] = [
                        ft.Text(
                            f"{float(line.get('quantity') or 0):g} × {line.get('name')}",
                            size=12, color=Colors.TEXT_PRIMARY, expand=True,
                        )
                    ]
                    if line.get("printed_at") and not line_ready:
                        def _mark_line(_=None, lid=int(line["line_id"])) -> None:
                            try:
                                ctx.sections.mark_lines_ready([int(lid)])
                            except Exception:
                                pass
                            render()
                        row_controls.append(ft.IconButton(
                            icon=ft.Icons.CHECK_CIRCLE_OUTLINE, tooltip="جاهز",
                            on_click=_mark_line, icon_size=18, icon_color=Colors.SUCCESS_DARK,
                        ))
                    elif line_ready:
                        row_controls.append(ft.Icon(ft.Icons.CHECK_CIRCLE, size=16, color=Colors.SUCCESS_DARK))
                    body.append(ft.Row(row_controls, spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER))
            if not ready:
                def _mark_ready(_=None, i=int(order["id"])) -> None:
                    try:
                        ctx.sections.mark_order_ready(int(i))
                    except Exception:
                        pass
                    render()
                body.append(ft.ElevatedButton("تم كل التحضير", icon=ft.Icons.DONE_ALL, on_click=_mark_ready))
            cards.append(ft.Container(
                ft.Column(body, spacing=4, tight=True),
                width=240, padding=12, bgcolor=Colors.WHITE,
                border=ft.border.all(2, Colors.SUCCESS_DARK if ready else _age_color(elapsed)),
                border_radius=Radius.LG, shadow=Shadow.SM,
            ))
        if not cards:
            cards.append(ft.Text("لا توجد طلبات قيد التحضير.", size=12, color=Colors.TEXT_SECONDARY))
        board.controls = cards
        try:
            board.update()
        except Exception:
            pass

    control = ft.Container(
        ft.Column([board], scroll=ft.ScrollMode.AUTO, expand=True),
        expand=True, padding=4, bgcolor=Colors.WHITE,
    )
    return control, render


def _sheet_board(page, ctx, notify, *, title: str, subtitle: str, icon, build) -> None:
    sheet, width = sheets.new_desktop_sheet(page, cap=1040)
    stop = {"flag": False}

    def close(_=None) -> None:
        stop["flag"] = True
        page.close(sheet)

    content, render = build()

    sheet.content = sheets.sheet_frame(
        ft.Column(
            [
                ft.Row([sheets.sheet_handle()], alignment=ft.MainAxisAlignment.CENTER),
                sheets.sheet_header(
                    title=title, subtitle=subtitle, icon=icon, on_close=close,
                    trailing=ft.IconButton(icon=ft.Icons.REFRESH, tooltip="تحديث", on_click=lambda _=None: render()),
                ),
                content,
            ],
            spacing=10, expand=True,
        ),
        width=width, height=sheets.sheet_height(page, 0.85),
    )
    render()
    sheet.on_dismiss = lambda e: stop.__setitem__("flag", True)
    page.open(sheet)
    _start_auto_refresh(page, render, stop)


def open_tables_board(page, ctx, notify, money, on_open_order=None, on_free_table=None, on_pay_order=None) -> None:
    use_tables = True
    try:
        from nano_offline.core import restaurant_settings
        use_tables = restaurant_settings.use_tables(ctx.settings)
    except Exception:
        use_tables = True

    def build():
        return tables_workspace(
            ctx, notify, money,
            on_open_order=on_open_order, on_free_table=on_free_table,
            on_pay_order=on_pay_order, use_tables=use_tables,
        )

    _sheet_board(
        page, ctx, notify,
        title="لوحة الطاولات" if use_tables else "الطلبات المفتوحة",
        subtitle="اللون يشير للحالة والتأخير",
        icon=ft.Icons.APPS_ROUNDED,
        build=build,
    )


def open_kitchen_board(page, ctx, notify) -> None:
    def build():
        return kitchen_workspace(ctx, notify)

    _sheet_board(
        page, ctx, notify,
        title="شاشة المطبخ",
        subtitle="تذاكر التحضير حسب القسم — اللون يشير لزمن الانتظار",
        icon=ft.Icons.RECEIPT_LONG_OUTLINED,
        build=build,
    )