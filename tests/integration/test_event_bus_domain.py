"""Domain services publish events on create/update/delete."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nano_offline.app_context import AppContext
from nano_offline.core import event_bus
from nano_offline.services.invoice_service import InvoiceLineInput


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-events-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def test_services_publish_create_update_delete_events():
    ctx, tmp = _ctx()
    seen: list[str] = []

    def listener(event, _payload):
        seen.append(event)

    bus = event_bus.bus()
    unsubs = [bus.subscribe(name, listener) for name in event_bus.MUTATION_EVENTS]
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        item = ctx.items.create(name="مادة", purchase_price=2.0, selling_price=3.0, quantity=0.0, base_unit_id=unit, barcode="EV-1")
        temporary_item = ctx.items.create(name="مؤقتة", item_type="خدمة", selling_price=1.0)
        customer = ctx.customers.create("عميل")
        customer2 = ctx.customers.create("عميل مؤقت")

        sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0, allow_negative_stock=True,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=1, unit_price=3.0, unit_id=unit)],
        )
        ctx.invoices.update_invoice(
            sale, invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=2, unit_price=3.0, unit_id=unit)],
        )
        voucher = ctx.payments.create_voucher(voucher_type="receipt", amount=2.0, customer_id=customer)
        ctx.payments.update_voucher(voucher, voucher_type="receipt", amount=1.0, customer_id=customer)
        expense = ctx.expenses.create_expense(amount=1.0, description="كهرباء")
        ctx.expenses.update_expense(expense, amount=1.5, description="كهرباء")

        ctx.items.update(item, name="مادة معدلة", barcode="EV-1", base_unit_id=unit, purchase_price=2.0, selling_price=3.0)
        ctx.customers.update(customer2, "عميل معدل")

        ctx.invoices.delete_invoice(sale)
        ctx.payments.delete_voucher(voucher)
        ctx.expenses.delete_expense(expense)
        ctx.items.delete(temporary_item)
        ctx.customers.delete(customer2)
    finally:
        for unsubscribe in unsubs:
            unsubscribe()
        shutil.rmtree(tmp, ignore_errors=True)

    for expected in (
        event_bus.ITEM_CREATED, event_bus.ITEM_UPDATED, event_bus.ITEM_DELETED,
        event_bus.PARTY_CREATED, event_bus.PARTY_UPDATED, event_bus.PARTY_DELETED,
        event_bus.INVOICE_CREATED, event_bus.INVOICE_UPDATED, event_bus.INVOICE_DELETED,
        event_bus.VOUCHER_CREATED, event_bus.VOUCHER_UPDATED, event_bus.VOUCHER_DELETED,
        event_bus.EXPENSE_CREATED, event_bus.EXPENSE_UPDATED, event_bus.EXPENSE_DELETED,
    ):
        assert expected in seen, f"missing {expected}; saw {sorted(set(seen))}"
