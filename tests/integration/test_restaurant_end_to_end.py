"""Comprehensive end-to-end: restaurant / sections / printing / accounting.

One test drives the whole lifecycle the way a real shift would, then asserts
the accounting and inventory invariants a POS must never break:

open order -> per-section kitchen tickets -> send added items -> void removed
items -> mark ready -> merge + split -> settle -> reports + queue.

It is intentionally an integration test (real AppContext/SQLite) so it also
guards the schema migrations and the derived-state rebuild.
"""

from __future__ import annotations

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import restaurant_settings
from nano_offline.core.audit_chain import verify_audit_chain
from nano_offline.services.invoice_service import InvoiceLineInput


def _ctx(tmp_path) -> AppContext:
    ctx = AppContext.create(tmp_path / "nano.db")
    ctx.section_print.auto_dispatch = False
    restaurant_settings.set_enabled(ctx.settings, True)
    restaurant_settings.set_use_tables(ctx.settings, True)
    return ctx


def _line(item_id, name, qty, price):
    return InvoiceLineInput(description=name, item_id=item_id, quantity=qty, unit_price=price)


def test_full_restaurant_lifecycle(tmp_path):
    ctx = _ctx(tmp_path)
    kitchen = ctx.sections.create(name="المطبخ", printer_host="10.0.0.1")
    drinks = ctx.sections.create(name="المشروبات", printer_host="10.0.0.2")

    burger = ctx.items.create(name="برغر", item_type="مخزون", quantity=100, purchase_price=4, selling_price=10, section_id=kitchen)
    salad = ctx.items.create(name="سلطة", item_type="مخزون", quantity=100, purchase_price=2, selling_price=5, section_id=kitchen)
    cola = ctx.items.create(name="كولا", item_type="مخزون", quantity=100, purchase_price=1, selling_price=3, section_id=drinks)

    date = ctx.items.create(name="تمر", item_type="مخزون", quantity=100, purchase_price=1, selling_price=2, section_id=kitchen)

    # Quick table setup by count (as the UI does).
    assert ctx.tables.create_many(area="صالة", start=1, count=3) == 3

    def qty(item_id: int) -> float:
        return float(ctx.items.get(item_id)["quantity"])

    # --- open an order (unpaid, no customer) ---
    inv = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[_line(burger, "برغر", 2, 10), _line(cola, "كولا", 3, 3)],
        paid_amount=0,
        reference="1",
    )
    row = ctx.invoices.get_invoice(inv)
    assert float(row["paid_amount"]) == 0
    assert row["customer_id"] is not None                       # system party
    # Stock consumed once.
    assert qty(burger) == pytest.approx(98)
    assert qty(cola) == pytest.approx(97)
    # One kitchen ticket per section, queued (2 sections).
    assert ctx.sections.counts()["pending"] == 2
    # Lines marked as sent.
    assert all(line.get("printed_at") for line in row["lines"])

    # --- add an item, then send only the new one ---
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[_line(burger, "برغر", 2, 10), _line(cola, "كولا", 3, 3), _line(salad, "سلطة", 1, 5)],
        customer_id=row["customer_id"],
        paid_amount=0,
        reference="1",
    )
    # Drain what is pending, then the incremental send must add exactly one.
    ctx.section_print.dispatch_pending(sender=lambda *a: None)
    assert ctx.section_print.send_new_invoice(inv) == 1
    ctx.section_print.dispatch_pending(sender=lambda *a: None)
    assert ctx.sections.counts()["pending"] == 0
    assert qty(salad) == pytest.approx(99)

    # --- remove an item -> a void ticket is queued ---
    before_voids = ctx.sections.counts()["sent"]
    ctx.invoices.update_invoice(
        inv,
        invoice_type="sale",
        lines=[_line(burger, "برغر", 2, 10), _line(salad, "سلطة", 1, 5)],
        customer_id=row["customer_id"],
        paid_amount=0,
        reference="1",
    )
    void_jobs = ctx.sections.pending_jobs()
    assert len(void_jobs) == 1
    assert "إلغاء".encode("cp1256") in bytes(void_jobs[0]["payload"])
    assert qty(cola) == pytest.approx(100)  # the removed cola came back in full
    ctx.section_print.dispatch_pending(sender=lambda *a: None)
    assert ctx.sections.counts()["sent"] == before_voids + 1

    # --- mark the order ready for the kitchen ---
    ctx.sections.mark_order_ready(inv)
    ready_lines = ctx.invoices.get_invoice(inv)["lines"]
    assert all(line.get("kitchen_ready_at") for line in ready_lines)

    # --- second order, then merge it into the first ---
    other = ctx.invoices.create_invoice(
        invoice_type="sale", lines=[_line(salad, "سلطة", 2, 5)], paid_amount=0, reference="2",
    )
    ctx.section_print.dispatch_pending(sender=lambda *a: None)
    ctx.invoices.merge_sale_orders(inv, [other])
    merged = ctx.invoices.get_invoice(inv)
    assert ctx.invoices.get_invoice(other) is None
    assert float(merged["total"]) == pytest.approx(2 * 10 + 1 * 5 + 2 * 5)  # 35

    # --- split one line out to a new order ---
    new_id = ctx.invoices.split_sale_order(inv, [0])  # move the burger line
    kept = ctx.invoices.get_invoice(inv)
    moved = ctx.invoices.get_invoice(new_id)
    assert float(moved["total"]) == pytest.approx(20)   # 2 burgers
    assert float(kept["total"]) == pytest.approx(15)    # 1 salad + 2 salads

    # --- settle both orders ---
    for target in (inv, new_id):
        current = ctx.invoices.get_invoice(target)
        ctx.invoices.update_invoice(
            target,
            invoice_type="sale",
            lines=[
                InvoiceLineInput(
                    description=str(line["description"]),
                    item_id=line["item_id"],
                    quantity=float(line["quantity"]),
                    unit_price=float(line["unit_price"]),
                    unit_id=line.get("unit_id"),
                    conversion_factor=float(line.get("conversion_factor") or 1),
                )
                for line in current["lines"]
            ],
            customer_id=current["customer_id"],
            paid_amount=float(current["total"]),
            reference=current["reference"],
        )
    assert ctx.invoices.open_sale_orders() == []

    # --- reports: kitchen revenue/cost/profit add up ---
    by_name = {r["section_name"]: r for r in ctx.sections.sales_by_section()}
    assert by_name["المطبخ"]["revenue"] == pytest.approx(2 * 10 + 1 * 5 + 2 * 5)  # burgers + salads
    assert by_name.get("المشروبات", {"revenue": 0})["revenue"] == pytest.approx(0)

    # --- invariants ---
    assert verify_audit_chain(ctx.db)["valid"] is True
    # inventory conservation: 100 opening minus what was actually sold
    assert qty(burger) == pytest.approx(100 - 2)
    assert qty(salad) == pytest.approx(100 - 3)
    assert qty(cola) == pytest.approx(100)
    assert qty(date) == pytest.approx(100)