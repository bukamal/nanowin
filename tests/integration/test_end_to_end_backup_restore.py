"""Backup -> mutate -> restore round-trip preserves the whole accounting state.

Complements the lifecycle test: after a real trading day, a backup is taken,
the database is mutated, then restored -- and every derived figure (dashboard,
inventory valuation, cash, ledger) must come back exactly, not just the raw
rows.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core.database import SCHEMA_VERSION
from nano_offline.services.invoice_service import InvoiceLineInput

PASSWORD = "StrongPass1"


def _new_ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-bak-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.auth.login("admin", PASSWORD)
    return ctx, tmp


def _trial_balance(ctx: AppContext) -> tuple[float, float]:
    with ctx.db.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(debit),0) AS d, COALESCE(SUM(credit),0) AS c FROM ledger_entries"
        ).fetchone()
    return float(row["d"]), float(row["c"])


def test_backup_restore_round_trip_preserves_accounting():
    ctx, tmp = _new_ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        category = ctx.definitions.create_category("مواد")
        customer = ctx.customers.create("عميل")
        supplier = ctx.suppliers.create("مورد")
        item = ctx.items.create(
            name="مادة", category_id=category, item_type="مخزون",
            purchase_price=10.0, selling_price=15.0, quantity=5.0,
            base_unit_id=unit, barcode="BK-1",
        )
        ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
        ctx.invoices.create_invoice(
            invoice_type="purchase", supplier_id=supplier,
            lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=4, unit_price=12.0, unit_id=unit)],
        )
        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=10.0,
            lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=3, unit_price=15.0, unit_id=unit)],
        )
        ctx.expenses.create_expense(amount=7.0, description="كهرباء")

        summary_before = ctx.dashboard.summary()
        snapshot = {
            "invoices": len(ctx.invoices.list_invoices()),
            "quantity": float(ctx.items.get(item)["quantity"]),
            "inventory_value": ctx.reports.inventory_valuation()["total_value"],
            "cash": ctx.reports.cash_movement()["closing_balance"],
        }

        plain = ctx.backup.create_backup(Path(tmp) / "snap.nanobackup")
        validation = ctx.backup.validate_backup(plain)
        assert validation.valid and validation.schema_version == SCHEMA_VERSION

        encrypted = ctx.backup.create_backup(Path(tmp) / "enc.nanobackup", password="pw1234")
        assert ctx.backup.validate_backup(encrypted, password="pw1234").valid
        with pytest.raises(ValueError, match="مشفرة"):
            ctx.backup.validate_backup(encrypted)  # password required

        # Mutate the live database after the backup.
        ctx.customers.create("عميل جديد")
        ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=1, unit_price=15.0, unit_id=unit)],
        )
        assert len(ctx.invoices.list_invoices()) == snapshot["invoices"] + 1

        safety = ctx.backup.restore_backup(plain)
        assert safety.exists()

        # Raw counts + every derived figure are back to the snapshot.
        assert len(ctx.invoices.list_invoices()) == snapshot["invoices"]
        assert ctx.items.get(item)["quantity"] == pytest.approx(snapshot["quantity"], abs=1e-6)
        assert ctx.reports.inventory_valuation()["total_value"] == pytest.approx(snapshot["inventory_value"], abs=1e-6)
        assert ctx.reports.cash_movement()["closing_balance"] == pytest.approx(snapshot["cash"], abs=1e-6)
        summary_after = ctx.dashboard.summary()
        for key in ("sales", "purchases", "cogs", "expenses", "net_profit", "inventory_value", "cash"):
            assert summary_after[key] == pytest.approx(summary_before[key], abs=1e-6)
        debit, credit = _trial_balance(ctx)
        assert debit == pytest.approx(credit, abs=1e-6)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)