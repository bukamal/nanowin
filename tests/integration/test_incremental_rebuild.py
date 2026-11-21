from __future__ import annotations

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.services.accounting_rebuilder import AccountingRebuilder
from nano_offline.services.invoice_service import InvoiceLineInput


def _snapshot(ctx) -> dict:
    with ctx.db.connect() as conn:
        def rows(sql):
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        return {
            "movements": sorted(rows(
                "SELECT item_id,COALESCE(invoice_id,0),movement_type,ROUND(quantity_delta,6),"
                "ROUND(unit_cost,6),ROUND(value_delta,6),movement_date FROM inventory_movements WHERE invoice_id IS NOT NULL"
            )),
            "items": sorted(rows(
                "SELECT id,ROUND(quantity,6),ROUND(average_cost,6),ROUND(purchase_price,6) FROM items"
            )),
            "lines": sorted(rows(
                "SELECT id,ROUND(unit_cost,6),ROUND(cost_amount,6) FROM invoice_lines"
            )),
            "ledger": sorted(rows(
                "SELECT entry_date,account_code,COALESCE(party_type,''),COALESCE(party_id,0),"
                "ROUND(debit,6),ROUND(credit,6),source_type,description "
                "FROM ledger_entries WHERE source_type IN ('invoice','payment','opening')"
            )),
            "payments": sorted(rows(
                "SELECT COALESCE(invoice_id,0),COALESCE(customer_id,0),COALESCE(supplier_id,0),"
                "direction,ROUND(amount,6),payment_date,source_type,COALESCE(source_id,0) FROM payments"
            )),
            "customers": sorted(rows("SELECT id,ROUND(balance,6) FROM customers")),
            "suppliers": sorted(rows("SELECT id,ROUND(balance,6) FROM suppliers")),
            "paid": sorted(rows("SELECT id,ROUND(paid_amount,6) FROM invoices")),
        }


def test_incremental_create_matches_full_rebuild():
    tmp = tempfile.mkdtemp(prefix="nano-incr-")
    try:
        ctx = AppContext.create(Path(tmp) / "nano.db")
        ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
        items = [
            ctx.items.create(name=f"مادة {i}", selling_price=2.0 + i, purchase_price=1.0 + i * 0.1, quantity=100)
            for i in range(4)
        ]
        cust = ctx.customers.create("عميل اختبار", "0900000000")
        sup = ctx.suppliers.create("مورد اختبار", "0911111111")

        base = date.today() - timedelta(days=30)
        for n in range(24):
            day = (base + timedelta(days=n)).isoformat()
            item_id = int(items[n % len(items)])
            if n % 3 == 0:
                ctx.invoices.create_invoice(
                    invoice_type="purchase", supplier_id=int(sup), invoice_date=day, paid_amount=0,
                    lines=[InvoiceLineInput(description="", item_id=item_id, quantity=10 + n, unit_price=1.0)],
                )
            else:
                ctx.invoices.create_invoice(
                    invoice_type="sale",
                    customer_id=int(cust) if n % 2 == 0 else None,
                    invoice_date=day,
                    paid_amount=1.0 if n % 4 == 0 else 0,
                    lines=[InvoiceLineInput(description="", item_id=item_id, quantity=1 + (n % 4), unit_price=3.0)],
                )
        incremental = _snapshot(ctx)

        with ctx.db.transaction() as conn:
            AccountingRebuilder.rebuild(conn)
        full = _snapshot(ctx)

        assert incremental == full
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_backdated_invoice_falls_back_and_matches_full_rebuild():
    tmp = tempfile.mkdtemp(prefix="nano-incr-back-")
    try:
        ctx = AppContext.create(Path(tmp) / "nano.db")
        ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
        item = ctx.items.create(name="مادة", selling_price=2.0, purchase_price=1.0, quantity=100)
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=10)).isoformat()
        ctx.invoices.create_invoice(
            invoice_type="sale", invoice_date=today, paid_amount=0,
            lines=[InvoiceLineInput(description="", item_id=int(item), quantity=5, unit_price=2.0)],
        )
        # Back-dated sale must still be posted correctly (falls back internally).
        ctx.invoices.create_invoice(
            invoice_type="sale", invoice_date=old, paid_amount=0,
            lines=[InvoiceLineInput(description="", item_id=int(item), quantity=2, unit_price=2.0)],
        )
        incremental = _snapshot(ctx)
        with ctx.db.transaction() as conn:
            AccountingRebuilder.rebuild(conn)
        full = _snapshot(ctx)
        assert incremental == full
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
