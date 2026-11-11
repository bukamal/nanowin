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