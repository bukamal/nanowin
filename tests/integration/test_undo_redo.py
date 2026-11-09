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


def test_voucher_create_update_delete_undo_redo():
    ctx, tmp = _ctx()
    _isolate()
    undo.install(ctx)
    try:
        customer = ctx.customers.create("عميل")
        undo.manager().clear()

        voucher = ctx.payments.create_voucher(voucher_type="receipt", amount=5.0, customer_id=customer)
        assert ctx.payments.get_voucher(voucher) is not None
        assert undo.undo() == "إنشاء سند"
        assert ctx.payments.get_voucher(voucher) is None
        assert undo.redo() == "إنشاء سند"

        created = next(v for v in ctx.payments.list_vouchers() if float(v["amount"]) == 5.0)
        vid = int(created["id"])
        undo.manager().clear()
        ctx.payments.update_voucher(vid, voucher_type="receipt", amount=3.0, customer_id=customer)
        assert float(ctx.payments.get_voucher(vid)["amount"]) == 3.0
        assert undo.undo() == "تعديل سند"
        assert float(ctx.payments.get_voucher(vid)["amount"]) == 5.0
        assert undo.redo() == "تعديل سند"
        assert float(ctx.payments.get_voucher(vid)["amount"]) == 3.0

        undo.manager().clear()
        ctx.payments.delete_voucher(vid)
        assert ctx.payments.get_voucher(vid) is None
        assert undo.undo() == "حذف سند"
        assert any(float(v["amount"]) == 3.0 for v in ctx.payments.list_vouchers())
        assert undo.redo() == "حذف سند"
        assert not any(float(v["amount"]) == 3.0 for v in ctx.payments.list_vouchers())
    finally:
        event_bus.bus().clear()
        undo.manager().clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_item_update_undo_restores_name_prices_and_units():
    ctx, tmp = _ctx()
    _isolate()
    undo.install(ctx)
    try:
        kg = ctx.definitions.create_unit("كيلو", "كغ")
        sack = ctx.definitions.create_unit("شوال", "ش")
        carton = ctx.definitions.create_unit("كرتونة", "ك")
        item = ctx.items.create(
            name="سكر", purchase_price=1.0, selling_price=1.4, quantity=0.0,
            base_unit_id=kg, barcode="IU-1",
            item_units=[{"unit_id": sack, "conversion_factor": 50, "purchase_price": 45.0, "selling_price": 60.0}],
        )
        undo.manager().clear()

        ctx.items.update(
            item, name="سكر فاخر", purchase_price=1.2, selling_price=1.8,
            base_unit_id=kg, barcode="IU-1",
            item_units=[{"unit_id": carton, "conversion_factor": 24, "purchase_price": 22.0, "selling_price": 30.0}],
        )
        after = ctx.items.get(item)
        assert after["name"] == "سكر فاخر"
        assert {int(u["id"]) for u in ctx.items.units(item) if not u.get("is_base")} == {int(carton)}

        assert undo.undo() == "تعديل مادة"
        restored = ctx.items.get(item)
        assert restored["name"] == "سكر"
        assert float(restored["purchase_price"]) == pytest.approx(1.0)
        assert {int(u["id"]) for u in ctx.items.units(item) if not u.get("is_base")} == {int(sack)}

        assert undo.redo() == "تعديل مادة"
        redone = ctx.items.get(item)
        assert redone["name"] == "سكر فاخر"
        assert {int(u["id"]) for u in ctx.items.units(item) if not u.get("is_base")} == {int(carton)}
    finally:
        event_bus.bus().clear()
        undo.manager().clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_item_create_and_delete_undo_redo():
    ctx, tmp = _ctx()
    _isolate()
    undo.install(ctx)
    try:
        unit = ctx.definitions.create_unit("كيلو", "كغ")

        # Service item with no stock/movements is cleanly reversible.
        undo.manager().clear()
        item = ctx.items.create(name="خدمة", item_type="خدمة", selling_price=1.0, base_unit_id=unit, barcode="IC-1")
        assert ctx.items.get(item) is not None
        assert undo.undo() == "إنشاء مادة"
        assert ctx.items.get(item) is None
        assert undo.redo() == "إنشاء مادة"
        assert any(i.get("barcode") == "IC-1" for i in ctx.items.list())

        # Deleting an unused item is reversible too.
        created = next(i for i in ctx.items.list() if i.get("barcode") == "IC-1")
        item_id = int(created["id"])
        undo.manager().clear()
        ctx.items.delete(item_id)
        assert ctx.items.get(item_id) is None
        assert undo.undo() == "حذف مادة"
        assert any(i.get("barcode") == "IC-1" for i in ctx.items.list())
        assert undo.redo() == "حذف مادة"
        assert not any(i.get("barcode") == "IC-1" for i in ctx.items.list())

        # A zero-stock item with adjustment-only history can be deleted, and
        # undo restores its movement audit trail along with the item.
        adjustable = ctx.items.create(name="تسويات فقط", purchase_price=1.0, base_unit_id=unit)
        ctx.items.update(
            adjustable, name="تسويات فقط", purchase_price=1.0,
            selling_price=2.0, base_unit_id=unit, quantity=5.0,
        )
        ctx.items.update(
            adjustable, name="تسويات فقط", purchase_price=1.0,
            selling_price=2.0, base_unit_id=unit, quantity=0.0,
        )
        assert len(ctx.items.movements(adjustable)) == 2
        undo.manager().clear()
        ctx.items.delete(adjustable)
        assert ctx.items.get(adjustable) is None
        assert undo.undo() == "حذف مادة"
        restored = next(i for i in ctx.items.list() if i.get("name") == "تسويات فقط")
        restored_id = int(restored["id"])
        assert len(ctx.items.movements(restored_id)) == 2
        assert undo.redo() == "حذف مادة"
        assert ctx.items.get(restored_id) is None

        # An item created WITH opening stock is not recorded (unsafe to undo
        # without opening-invoice handling).
        undo.manager().clear()
        ctx.items.create(name="بكمية", purchase_price=1.0, selling_price=2.0, quantity=5.0, base_unit_id=unit, barcode="IC-2")
        assert undo.manager().can_undo() is False
    finally:
        event_bus.bus().clear()
        undo.manager().clear()
        shutil.rmtree(tmp, ignore_errors=True)
