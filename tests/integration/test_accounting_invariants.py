"""The shared accounting invariants, exercised as a test.

Builds one small mixed scenario (opening + purchase + partial-paid sale +
voucher + expense) and asserts the central invariant suite holds, and that a
rebuild changes nothing. New workflow tests can import the same helpers.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import invariants  # noqa: E402

from nano_offline.app_context import AppContext  # noqa: E402
from nano_offline.services.invoice_service import InvoiceLineInput  # noqa: E402


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-inv-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def test_shared_invariants_hold_after_a_mixed_cycle():
    ctx, tmp = _ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        customer = ctx.customers.create("عميل")
        supplier = ctx.suppliers.create("مورد")
        item = ctx.items.create(
            name="مادة", purchase_price=10.0, selling_price=15.0, quantity=10.0,
            base_unit_id=unit, barcode="INV-1",
        )
        ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
        ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=6, unit_price=12.0, unit_id=unit)],
        )
        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=10.0,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=4, unit_price=15.0, unit_id=unit)],
        )
        sale = ctx.invoices.list_invoices(invoice_type="sale")[0]["id"]
        ctx.payments.create_voucher(
            voucher_type="receipt", amount=20.0, customer_id=customer,
            allocation_mode="manual", allocations={sale: 20.0},
        )
        ctx.expenses.create_expense(amount=5.0, description="كهرباء")

        invariants.assert_all(ctx)
        invariants.assert_rebuild_idempotent(ctx)
        # Still healthy after the rebuild round-trip.
        invariants.assert_all(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)