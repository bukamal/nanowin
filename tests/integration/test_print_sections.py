"""Print sections (stations) + network ESC/POS queue (schema 19).

Restaurant model: each item/line resolves to a section (item override, then
category default) and that section's sale lines print as one price-less ticket
on the section's network printer. Jobs are queued first and dispatched
separately, so printing never blocks the sale.
"""

from __future__ import annotations

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import escpos
from nano_offline.core import restaurant_settings
from nano_offline.services.invoice_service import InvoiceLineInput


def _ctx(tmp_path) -> AppContext:
    ctx = AppContext.create(tmp_path / "nano.db")
    ctx.section_print.auto_dispatch = False  # no sockets in tests
    return ctx


def test_escpos_ticket_contains_section_and_items():
    payload = escpos.build_station_ticket(
        {"name": "المطبخ", "paper_width_mm": 80, "encoding": "cp1256", "copies": 1},
        {"id": 7, "reference": "INV-1", "invoice_date": "2026-01-01", "party_name": "علي", "notes": ""},
        [{"name": "برغر", "quantity": 2}],
    )
    assert payload.startswith(b"\x1b@")           # init
    assert b"\x1dV\x00" in payload                 # cut
    assert "المطبخ".encode("cp1256") in payload
    assert "برغر".encode("cp1256") in payload


def test_section_crud_and_validation(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="192.168.1.50", printer_port=9100)
    section = ctx.sections.get(sid)
    assert section["name"] == "المطبخ"
    assert section["printer_host"] == "192.168.1.50"

    ctx.sections.update(sid, name="المطبخ الرئيسي", printer_host="192.168.1.50", paper_width_mm=58, copies=2)
    section = ctx.sections.get(sid)
    assert section["paper_width_mm"] == 58 and section["copies"] == 2

    try:
        ctx.sections.create(name="خطأ", paper_width_mm=42)
        raise AssertionError("invalid width must be rejected")
    except ValueError:
        pass

    ctx.sections.delete(sid)
    assert ctx.sections.get(sid) is None


def test_item_then_category_section_resolution(tmp_path):
    ctx = _ctx(tmp_path)
    kitchen = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    bar = ctx.sections.create(name="البار", printer_host="10.0.0.2")
    cat_id = ctx.definitions.create_category("مشروبات") if hasattr(ctx.definitions, "create_category") else None
    if cat_id is not None:
        ctx.sections.set_category_section(int(cat_id), bar)

    item_id = ctx.items.create(name="عصير", item_type="مخزون", selling_price=5, category_id=cat_id, section_id=kitchen)
    items = {i["id"]: i for i in ctx.items.list()}
    assert int(items[item_id]["section_id"]) == kitchen  # item override wins


def test_sale_prints_one_ticket_per_section(tmp_path):
    ctx = _ctx(tmp_path)
    kitchen = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    bar = ctx.sections.create(name="البار", printer_host="10.0.0.2")
    burger = ctx.items.create(name="برغر", item_type="مخزون", selling_price=10, section_id=kitchen)
    fries = ctx.items.create(name="بطاطا", item_type="مخزون", selling_price=4, section_id=kitchen)
    cola = ctx.items.create(name="كولا", item_type="مخزون", selling_price=3, section_id=bar)
    unassigned = ctx.items.create(name="منديل", item_type="مخزون", selling_price=1)

    ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[
            InvoiceLineInput(description="برغر", item_id=burger, quantity=2, unit_price=10),
            InvoiceLineInput(description="بطاطا", item_id=fries, quantity=1, unit_price=4),
            InvoiceLineInput(description="كولا", item_id=cola, quantity=3, unit_price=3),
            InvoiceLineInput(description="منديل", item_id=unassigned, quantity=1, unit_price=1),
        ],
        paid_amount=2 * 10 + 4 + 3 * 3 + 1,
        allow_negative_stock=True,
    )

    jobs = ctx.sections.counts()
    assert jobs["pending"] == 2  # kitchen + bar (unassigned item prints nowhere)
    assert jobs["sent"] == 0 and jobs["failed"] == 0


def test_enqueue_is_deduplicated(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    item = ctx.items.create(name="برغر", item_type="مخزون", selling_price=10, section_id=sid)
    inv = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="برغر", item_id=item, quantity=1, unit_price=10)],
        paid_amount=10,
        allow_negative_stock=True,
    )
    assert ctx.section_print.enqueue_invoice(inv) == 0  # already enqueued on save
    assert ctx.sections.counts()["pending"] == 1


def test_reprint_invoice_replaces_previous_jobs(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    item = ctx.items.create(name="شوربة", item_type="مخزون", selling_price=5, section_id=sid)
    inv = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="شوربة", item_id=item, quantity=1, unit_price=5)],
        paid_amount=5,
        allow_negative_stock=True,
    )
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: None)
    assert ctx.sections.counts()["sent"] == 1

    assert ctx.section_print.reprint_invoice(inv) == 1
    counts = ctx.sections.counts()
    assert counts["pending"] == 1 and counts["sent"] == 0


def test_sales_by_section_totals(tmp_path):
    ctx = _ctx(tmp_path)
    kitchen = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    bar = ctx.sections.create(name="البار", printer_host="10.0.0.2")
    burger = ctx.items.create(
        name="برغر", item_type="مخزون", quantity=100, purchase_price=5, selling_price=10, section_id=kitchen
    )
    cola = ctx.items.create(
        name="كولا", item_type="مخزون", quantity=100, purchase_price=1, selling_price=3, section_id=bar
    )
    napkin = ctx.items.create(name="منديل", item_type="مخزون", quantity=100, purchase_price=0.5, selling_price=1)

    ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[
            InvoiceLineInput(description="برغر", item_id=burger, quantity=2, unit_price=10),
            InvoiceLineInput(description="كولا", item_id=cola, quantity=3, unit_price=3),
            InvoiceLineInput(description="منديل", item_id=napkin, quantity=1, unit_price=1),
        ],
        paid_amount=30,
    )

    by_name = {r["section_name"]: r for r in ctx.sections.sales_by_section()}
    assert by_name["المطبخ"]["revenue"] == pytest.approx(20)
    assert by_name["المطبخ"]["cost"] == pytest.approx(10)     # 2 x avg cost 5
    assert by_name["المطبخ"]["profit"] == pytest.approx(10)
    assert by_name["البار"]["revenue"] == pytest.approx(9)
    assert by_name["البار"]["cost"] == pytest.approx(3)       # 3 x avg cost 1
    # Unassigned items roll up under the NULL section (labelled by the UI).
    assert by_name[None]["revenue"] == pytest.approx(1)


def test_dispatch_success_and_failure_retry(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    item = ctx.items.create(name="برغر", item_type="مخزون", selling_price=10, section_id=sid)
    ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="برغر", item_id=item, quantity=1, unit_price=10)],
        paid_amount=10,
        allow_negative_stock=True,
    )

    sent_calls: list[tuple] = []
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: sent_calls.append((h, p, d)))
    assert sent_calls and sent_calls[0][0] == "10.0.0.1" and sent_calls[0][1] == 9100
    assert ctx.sections.counts()["sent"] == 1

    # Second invoice whose printer is down: stays pending, then failed after max attempts.
    item2 = ctx.items.create(name="سلطة", item_type="مخزون", selling_price=6, section_id=sid)
    ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="سلطة", item_id=item2, quantity=1, unit_price=6)],
        paid_amount=6,
        allow_negative_stock=True,
    )

    def _boom(h, p, d):
        raise OSError("printer offline")

    for _ in range(3):
        ctx.section_print.dispatch_pending(sender=_boom)
    counts = ctx.sections.counts()
    assert counts["failed"] == 1

    ctx.section_print.retry_failed()
    assert ctx.sections.counts()["pending"] == 1

def test_restaurant_mode_keeps_unpaid_order_open(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    item = ctx.items.create(
        name="بيتزا", item_type="مخزون", quantity=100, purchase_price=2, selling_price=10, section_id=sid
    )

    # Mode off: an anonymous sale with paid=0 is treated as fully-paid cash
    # (the long-standing non-restaurant rule).
    inv_cash = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="بيتزا", item_id=item, quantity=1, unit_price=10)],
        paid_amount=0,
    )
    assert float(ctx.invoices.get_invoice(inv_cash)["paid_amount"]) == pytest.approx(10)
    assert ctx.invoices.open_sale_orders() == []

    # Mode on: the same input becomes an open preparation order (paid=0),
    # attributed to the shared party, with a kitchen ticket queued.
    restaurant_settings.set_enabled(ctx.settings, True)
    inv_open = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="بيتزا", item_id=item, quantity=1, unit_price=10)],
        paid_amount=0,
    )
    row = ctx.invoices.get_invoice(inv_open)
    assert float(row["paid_amount"]) == pytest.approx(0)
    assert row["customer_id"] is not None
    assert [o["id"] for o in ctx.invoices.open_sale_orders()] == [inv_open]
    assert "طلبات مفتوحة" in {c["name"] for c in ctx.customers.list()}
    assert ctx.sections.counts()["pending"] >= 1


def test_send_new_items_only_after_initial_send(tmp_path):
    ctx = _ctx(tmp_path)
    restaurant_settings.set_enabled(ctx.settings, True)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    pizza = ctx.items.create(name="بيتزا", item_type="مخزون", quantity=100, selling_price=10, section_id=sid)
    salad = ctx.items.create(name="سلطة", item_type="مخزون", quantity=100, selling_price=6, section_id=sid)

    inv = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="بيتزا", item_id=pizza, quantity=1, unit_price=10)],
        paid_amount=0,
        reference="طاولة 4",
    )
    # The order printed on creation, so nothing is "new".
    assert ctx.section_print.send_new_invoice(inv) == 0

    order = next(o for o in ctx.invoices.open_sale_orders() if o["id"] == inv)
    assert order["reference"] == "طاولة 4"

    # Add a second item: editing recreates the lines as unsent, so the next
    # incremental send must print again exactly once.
    customer_id = int(ctx.invoices.get_invoice(inv)["customer_id"])
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[
            InvoiceLineInput(description="بيتزا", item_id=pizza, quantity=1, unit_price=10),
            InvoiceLineInput(description="سلطة", item_id=salad, quantity=1, unit_price=6),
        ],
        customer_id=customer_id,
        paid_amount=0,
        reference="طاولة 4",
    )
    assert ctx.section_print.send_new_invoice(inv) == 1
    assert ctx.section_print.send_new_invoice(inv) == 0


def _open_order(ctx, items, reference):
    return ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[
            InvoiceLineInput(description=name, item_id=item_id, quantity=qty, unit_price=price)
            for item_id, name, qty, price in items
        ],
        paid_amount=0,
        reference=reference,
    )


def test_merge_open_orders_combines_and_deletes_sources(tmp_path):
    ctx = _ctx(tmp_path)
    restaurant_settings.set_enabled(ctx.settings, True)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    tea = ctx.items.create(name="شاي", item_type="مخزون", quantity=100, purchase_price=1, selling_price=2, section_id=sid)
    cake = ctx.items.create(name="كعك", item_type="مخزون", quantity=100, purchase_price=2, selling_price=6, section_id=sid)

    a = _open_order(ctx, [(tea, "شاي", 2, 2)], "طاولة 1")
    b = _open_order(ctx, [(cake, "كعك", 3, 6)], "طاولة 2")

    ctx.invoices.merge_sale_orders(a, [b])

    merged = ctx.invoices.get_invoice(a)
    assert merged is not None
    assert float(merged["total"]) == pytest.approx(22)   # 2*2 + 3*6
    assert len(merged["lines"]) == 2
    assert ctx.invoices.get_invoice(b) is None
    assert [o["id"] for o in ctx.invoices.open_sale_orders()] == [a]

    # Stock consumed once for each original line, still after the rebuild.
    items = {i["name"]: i for i in ctx.items.list()}
    assert float(items["شاي"]["quantity"]) == pytest.approx(98)
    assert float(items["كعك"]["quantity"]) == pytest.approx(97)


def test_split_open_order_moves_selected_lines(tmp_path):
    ctx = _ctx(tmp_path)
    restaurant_settings.set_enabled(ctx.settings, True)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    tea = ctx.items.create(name="شاي", item_type="مخزون", quantity=100, purchase_price=1, selling_price=2, section_id=sid)
    cake = ctx.items.create(name="كعك", item_type="مخزون", quantity=100, purchase_price=2, selling_price=6, section_id=sid)
    juice = ctx.items.create(name="عصير", item_type="مخزون", quantity=100, purchase_price=1, selling_price=4, section_id=sid)

    src = _open_order(
        ctx,
        [(tea, "شاي", 1, 2), (cake, "كعك", 2, 6), (juice, "عصير", 1, 4)],
        "طاولة 9",
    )
    new_id = ctx.invoices.split_sale_order(src, [1])  # move the cake line

    kept = ctx.invoices.get_invoice(src)
    moved = ctx.invoices.get_invoice(new_id)
    assert float(kept["total"]) == pytest.approx(6)    # 1*2 + 1*4
    assert len(kept["lines"]) == 2
    assert float(moved["total"]) == pytest.approx(12)  # 2*6
    assert len(moved["lines"]) == 1
    assert moved["reference"].endswith("تقسيم")
    assert {o["id"] for o in ctx.invoices.open_sale_orders()} == {src, new_id}


def test_editing_open_order_prints_void_ticket(tmp_path):
    ctx = _ctx(tmp_path)
    restaurant_settings.set_enabled(ctx.settings, True)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    tea = ctx.items.create(name="شاي", item_type="مخزون", quantity=100, purchase_price=1, selling_price=2, section_id=sid)
    cake = ctx.items.create(name="كعك", item_type="مخزون", quantity=100, purchase_price=2, selling_price=6, section_id=sid)

    inv = _open_order(ctx, [(tea, "شاي", 2, 2), (cake, "كعك", 3, 6)], "طاولة 5")
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: None)
    assert ctx.sections.counts()["pending"] == 0

    customer_id = int(ctx.invoices.get_invoice(inv)["customer_id"])
    # Remove the cake line -> one void ticket for the kitchen.
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[InvoiceLineInput(description="شاي", item_id=tea, quantity=2, unit_price=2)],
        customer_id=customer_id,
        paid_amount=0,
        reference="طاولة 5",
    )
    pending = ctx.sections.pending_jobs()
    assert len(pending) == 1
    payload = bytes(pending[0]["payload"])
    assert "إلغاء".encode("cp1256") in payload
    assert "كعك".encode("cp1256") in payload


def test_editing_open_order_voids_only_reduced_quantity(tmp_path):
    ctx = _ctx(tmp_path)
    restaurant_settings.set_enabled(ctx.settings, True)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    tea = ctx.items.create(name="شاي", item_type="مخزون", quantity=100, purchase_price=1, selling_price=2, section_id=sid)

    inv = _open_order(ctx, [(tea, "شاي", 5, 2)], "طاولة 6")
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: None)

    customer_id = int(ctx.invoices.get_invoice(inv)["customer_id"])
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[InvoiceLineInput(description="شاي", item_id=tea, quantity=3, unit_price=2)],
        customer_id=customer_id,
        paid_amount=0,
        reference="طاولة 6",
    )
    pending = ctx.sections.pending_jobs()
    assert len(pending) == 1
    assert "إلغاء".encode("cp1256") in bytes(pending[0]["payload"])

    # No further change -> no additional void.
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: None)
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[InvoiceLineInput(description="شاي", item_id=tea, quantity=3, unit_price=2)],
        customer_id=customer_id,
        paid_amount=0,
        reference="طاولة 6",
    )
    assert ctx.sections.pending_jobs() == []


def test_sales_by_section_date_filter(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    item = ctx.items.create(name="شوربة", item_type="مخزون", quantity=100, purchase_price=1, selling_price=5, section_id=sid)
    ctx.invoices.create_invoice(
        invoice_type="sale",
        invoice_date="2026-01-15",
        lines=[InvoiceLineInput(description="شوربة", item_id=item, quantity=2, unit_price=5)],
        paid_amount=10,
    )

    inside = ctx.sections.sales_by_section(date_from="2026-01-01", date_to="2026-01-31")
    assert sum(r["revenue"] for r in inside) == pytest.approx(10)
    outside = ctx.sections.sales_by_section(date_from="2026-02-01", date_to="2026-02-28")
    assert outside == []


def test_section_fallback_printer_on_repeated_failure(tmp_path):
    ctx = _ctx(tmp_path)
    primary = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    backup = ctx.sections.create(name="الاحتياطي", printer_host="10.0.0.2")
    ctx.sections.update(primary, name="المطبخ", printer_host="10.0.0.1", fallback_section_id=backup)
    assert ctx.sections.fallback_for(primary)["id"] == backup

    item = ctx.items.create(name="بيتزا", item_type="مخزون", quantity=100, selling_price=10, section_id=primary)
    _open_order(ctx, [(item, "بيتزا", 1, 10)], "طاولة 1")

    reached: list[str] = []

    def sender(host, port, data):
        if host == "10.0.0.1":
            raise OSError("primary printer down")
        reached.append(host)

    for _ in range(3):
        ctx.section_print.dispatch_pending(sender=sender)

    assert "10.0.0.2" in reached
    counts = ctx.sections.counts()
    assert counts["failed"] == 1   # the primary job is recorded as failed
    assert counts["sent"] == 1     # the fallback delivery succeeded


def test_edit_then_send_new_prints_only_the_added_item(tmp_path):
    ctx = _ctx(tmp_path)
    restaurant_settings.set_enabled(ctx.settings, True)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    pizza = ctx.items.create(name="بيتزا", item_type="مخزون", quantity=100, selling_price=10, section_id=sid)
    salad = ctx.items.create(name="سلطة", item_type="مخزون", quantity=100, selling_price=6, section_id=sid)

    inv = _open_order(ctx, [(pizza, "بيتزا", 1, 10)], "طاولة 2")
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: None)

    customer_id = int(ctx.invoices.get_invoice(inv)["customer_id"])
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[
            InvoiceLineInput(description="بيتزا", item_id=pizza, quantity=1, unit_price=10),
            InvoiceLineInput(description="سلطة", item_id=salad, quantity=1, unit_price=6),
        ],
        customer_id=customer_id,
        paid_amount=0,
        reference="طاولة 2",
    )
    assert ctx.section_print.send_new_invoice(inv) == 1
    pending = ctx.sections.pending_jobs()
    assert len(pending) == 1
    payload = bytes(pending[0]["payload"])
    assert "سلطة".encode("cp1256") in payload
    assert "بيتزا".encode("cp1256") not in payload  # unchanged line not reprinted


def test_edit_without_changes_sends_nothing_new(tmp_path):
    ctx = _ctx(tmp_path)
    restaurant_settings.set_enabled(ctx.settings, True)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    pizza = ctx.items.create(name="بيتزا", item_type="مخزون", quantity=100, selling_price=10, section_id=sid)

    inv = _open_order(ctx, [(pizza, "بيتزا", 1, 10)], "طاولة 3")
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: None)
    customer_id = int(ctx.invoices.get_invoice(inv)["customer_id"])
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[InvoiceLineInput(description="بيتزا", item_id=pizza, quantity=1, unit_price=10)],
        customer_id=customer_id,
        paid_amount=0,
        reference="طاولة 3",
    )
    assert ctx.section_print.send_new_invoice(inv) == 0


def test_device_printer_override_takes_precedence(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    item = ctx.items.create(name="بيتزا", item_type="مخزون", quantity=100, selling_price=10, section_id=sid)
    _open_order(ctx, [(item, "بيتزا", 1, 10)], "طاولة 8")

    reached: list[tuple] = []
    # No override yet -> the shared section target is used.
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: reached.append((h, p)))
    assert reached == [("10.0.0.1", 9100)]

    # Override this device to a different printer.
    ctx.section_print.set_device_override(sid, "10.9.9.9:9101")
    assert ctx.section_print.device_override(sid) == "10.9.9.9:9101"

    reached.clear()
    _open_order(ctx, [(item, "بيتزا", 1, 10)], "طاولة 8ب")
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: reached.append((h, p)))
    assert reached == [("10.9.9.9", 9101)]

    # Clearing falls back to the shared target.
    ctx.section_print.set_device_override(sid, "")
    reached.clear()
    _open_order(ctx, [(item, "بيتزا", 1, 10)], "طاولة 8ج")
    ctx.section_print.dispatch_pending(sender=lambda h, p, d: reached.append((h, p)))
    assert reached == [("10.0.0.1", 9100)]


def test_section_ticket_preview_html(tmp_path):
    ctx = _ctx(tmp_path)
    html = ctx.documents.section_ticket_preview_html(
        {"name": "المطبخ", "paper_width_mm": 80},
        [{"name": "برغر", "quantity": 2}, {"name": "برغر", "quantity": 1, "void": True}],
        reference="طاولة 1",
        invoice_no=5,
    )
    assert "المطبخ" in html and "برغر" in html
    assert "طاولة 1" in html and "#5" in html
    assert "إلغاء" in html            # void line marked
    assert "$" not in html and "سعر" not in html  # prices intentionally omitted


def test_mark_order_ready_sets_line_marker(tmp_path):
    ctx = _ctx(tmp_path)
    sid = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    item = ctx.items.create(name="بيتزا", item_type="مخزون", quantity=100, selling_price=10, section_id=sid)
    inv = _open_order(ctx, [(item, "بيتزا", 1, 10)], "طاولة 1")

    ctx.sections.mark_order_ready(inv)
    lines = ctx.invoices.get_invoice(inv)["lines"]
    assert lines and all(line.get("kitchen_ready_at") for line in lines)


def test_restaurant_tables_crud(tmp_path):
    ctx = _ctx(tmp_path)
    tid = ctx.tables.create(name="1", area="صالة")
    ctx.tables.create(name="2", area="تراس")
    ctx.tables.update(tid, name="1", area="تراس", sort_order=2)
    names = [t["name"] for t in ctx.tables.list()]
    assert "1" in names and "2" in names
    assert ctx.tables.get(tid)["area"] == "تراس"
    ctx.tables.delete(tid)
    assert ctx.tables.get(tid) is None


def test_reorder_tables_moves_into_target_area_before_it(tmp_path):
    ctx = _ctx(tmp_path)
    a = ctx.tables.create(name="1", area="صالة", sort_order=0)
    b = ctx.tables.create(name="2", area="صالة", sort_order=1)
    c = ctx.tables.create(name="3", area="تراس", sort_order=0)

    ctx.tables.reorder(c, a)  # move table 3 into صالة, just before table 1
    hall = [t["name"] for t in ctx.tables.list() if t["area"] == "صالة"]
    assert hall == ["3", "1", "2"]
    assert ctx.tables.get(c)["area"] == "صالة"
    _ = b


def test_create_many_tables_by_count(tmp_path):
    ctx = _ctx(tmp_path)
    created = ctx.tables.create_many(area="صالة", start=1, count=5)
    assert created == 5
    assert [t["name"] for t in ctx.tables.list() if t["area"] == "صالة"] == ["1", "2", "3", "4", "5"]
    # Re-running skips existing numbers.
    assert ctx.tables.create_many(area="صالة", start=1, count=3) == 0
    assert ctx.tables.create_many(area="تراس", start=6, count=2, prefix="ط-") == 2
