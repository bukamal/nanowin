"""Session undo/redo for invoices, driven by the domain event bus."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import event_bus, undo
from nano_offline.services.invoice_service import InvoiceLineInput


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-undo-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def _setup(ctx):
    unit = ctx.definitions.create_unit("قطعة", "ق")
    customer = ctx.customers.create("عميل")
    item = ctx.items.create(
        name="مادة", purchase_price=2.0, selling_price=3.0, quantity=10.0,
        base_unit_id=unit, barcode="UNDO-1",
    )
    ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
    return unit, customer, item


def _sell(ctx, unit, customer, item, qty=1) -> int:
    return ctx.invoices.create_invoice(
        invoice_type="sale", customer_id=customer, paid_amount=0,
        lines=[InvoiceLineInput(description="x", item_id=item, quantity=qty, unit_price=3.0, unit_id=unit)],
    )


def _sales(ctx):
    return [i for i in ctx.invoices.list_invoices(invoice_type="sale")]


def _isolate():
    event_bus.bus().clear()
    undo.manager().clear()


def test_create_undo_redo():
    ctx, tmp = _ctx()
    _isolate()
    undo.install(ctx)
    try:
        unit, customer, item = _setup(ctx)
        undo.manager().clear()
        invoice_id = _sell(ctx, unit, customer, item)
        assert len(_sales(ctx)) == 1

        assert undo.undo() == "إنشاء فاتورة"
        assert ctx.invoices.get_invoice(invoice_id) is None
        assert len(_sales(ctx)) == 0
        assert undo.manager().can_redo()

        assert undo.redo() == "إنشاء فاتورة"
        assert len(_sales(ctx)) == 1


        from nano_offline.core import currency  # noqa: F401
    finally:
        event_bus.bus().clear()
        undo.manager().clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_undo_redo_restores_quantity():
    ctx, tmp = _ctx()
    _isolate()
    undo.install(ctx)
    try:
        unit, customer, item = _setup(ctx)
        invoice_id = _sell(ctx, unit, customer, item, qty=1)
        undo.manager().clear()

        ctx.invoices.update_invoice(
            invoice_id, invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=2, unit_price=3.0, unit_id=unit)],
        )
        assert float(ctx.invoices.get_invoice(invoice_id)["lines"][0]["quantity"]) == 2.0

        assert undo.undo() == "تعديل فاتورة"
        assert float(ctx.invoices.get_invoice(invoice_id)["lines"][0]["quantity"]) == 1.0

        assert undo.redo() == "تعديل فاتورة"
        assert float(ctx.invoices.get_invoice(invoice_id)["lines"][0]["quantity"]) == 2.0
    finally:
        event_bus.bus().clear()
        undo.manager().clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_delete_undo_recreates_and_redo_deletes():
    ctx, tmp = _ctx()
    _isolate()
    undo.install(ctx)
    try:
        unit, customer, item = _setup(ctx)
        invoice_id = _sell(ctx, unit, customer, item)
        undo.manager().clear()

        ctx.invoices.delete_invoice(invoice_id)
        assert len(_sales(ctx)) == 0

        assert undo.undo() == "حذف فاتورة"
        restored = _sales(ctx)
        assert len(restored) == 1
        assert float(restored[0]["total"]) == pytest.approx(3.0, abs=1e-6)

        assert undo.redo() == "حذف فاتورة"
        assert len(_sales(ctx)) == 0
    finally:
        event_bus.bus().clear()
        undo.manager().clear()
        shutil.rmtree(tmp, ignore_errors=True)
