"""Integration tests: real AppContext/DB, repositories (sync + .aio), audit, backup."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core.audit_chain import verify_audit_chain
from nano_offline.services.invoice_service import InvoiceLineInput


@pytest.fixture()
def ctx(tmp_path):
    return AppContext.create(tmp_path / "nano.db")


@pytest.fixture()
def seeded(ctx):
    ctx.definitions.create_unit("قطعة", "ق")
    ctx.customers.create("عميل", "0900000000")
    ctx.suppliers.create("مورد")
    item_id = ctx.items.create(name="مادة A", purchase_price=10, selling_price=15, quantity=10)
    return {"item_id": item_id}


def test_item_repository_sync(ctx, seeded):
    rows = ctx.items.list()
    assert any(r["id"] == seeded["item_id"] for r in rows)


def test_item_repository_async_bridge(ctx, seeded):
    async def scenario():
        return await ctx.items.aio.list()

    rows = asyncio.run(scenario())
    assert any(r["id"] == seeded["item_id"] for r in rows)
    # bridge exposes only @blocking methods
    with pytest.raises(AttributeError, match="not tagged @blocking"):
        ctx.items.aio._check_barcode_free


def test_party_repository_async(ctx, seeded):
    async def scenario():
        return await ctx.customers.aio.list()

    rows = asyncio.run(scenario())
    assert rows and rows[0]["name"] == "عميل"


def test_reporting_and_forecast_services_support_async_offload(ctx):
    async def scenario():
        pnl, cash, forecast = await asyncio.gather(
            ctx.reports.aio.income_statement(),
            ctx.reports.aio.cash_movement(),
            ctx.forecast.aio.seasonal_forecast(invoice_type="sale"),
        )
        return pnl, cash, forecast

    pnl, cash, forecast = asyncio.run(scenario())
    assert pnl["sales"] == 0
    assert cash["rows"] == []
    assert forecast["insufficient"] is True


def test_invoice_flow_updates_state(ctx, seeded):
    supplier_id = ctx.suppliers.list()[0]["id"]
    customer_id = ctx.customers.list()[0]["id"]
    ctx.invoices.create_invoice(
        invoice_type="purchase", supplier_id=supplier_id, paid_amount=20,
        lines=[InvoiceLineInput(description="A", item_id=seeded["item_id"], quantity=5, unit_price=12)],
    )
    item = ctx.items.get(seeded["item_id"])
    assert round(item["quantity"], 6) == 15
    assert round(ctx.suppliers.get(supplier_id)["balance"], 6) == 40
    ctx.invoices.create_invoice(
        invoice_type="sale", customer_id=customer_id, paid_amount=10,
        lines=[InvoiceLineInput(description="A", item_id=seeded["item_id"], quantity=3, unit_price=20)],
    )
    assert round(ctx.items.get(seeded["item_id"])["quantity"], 6) == 12
    assert round(ctx.customers.get(customer_id)["balance"], 6) == 50
    assert ctx.db.integrity_check() == "ok"


def test_audit_chain_valid_after_writes(ctx, seeded):
    supplier_id = ctx.suppliers.list()[0]["id"]
    ctx.invoices.create_invoice(
        invoice_type="purchase", supplier_id=supplier_id, paid_amount=5,
        lines=[InvoiceLineInput(description="A", item_id=seeded["item_id"], quantity=1, unit_price=12)],
    )
    result = verify_audit_chain(ctx.db)
    assert result["valid"] is True
    assert result["hashed"] >= 1


def test_audit_entries_support_offset_pagination(ctx):
    ctx.auth.create_user(
        username="admin",
        full_name="مدير",
        password="test-password-123",
        role="admin",
        _bootstrap=True,
    )
    ctx.auth.login("admin", "test-password-123")
    with ctx.db.transaction() as conn:
        for index in range(5):
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,details) VALUES('create','item',?)",
                (f"entry-{index}",),
            )

    first_page = ctx.auth.audit_entries(limit=3)
    second_page = ctx.auth.audit_entries(limit=3, offset=3)

    assert len(first_page) == 3
    assert len(second_page) == 3
    assert first_page[0]["id"] > first_page[-1]["id"] > second_page[0]["id"]
    assert {row["id"] for row in first_page}.isdisjoint(row["id"] for row in second_page)


def test_audit_chain_detects_tampering(ctx, seeded):
    supplier_id = ctx.suppliers.list()[0]["id"]
    ctx.invoices.create_invoice(
        invoice_type="purchase", supplier_id=supplier_id, paid_amount=5,
        lines=[InvoiceLineInput(description="A", item_id=seeded["item_id"], quantity=1, unit_price=12)],
    )
    assert verify_audit_chain(ctx.db)["valid"] is True
    # Silently edit a historical audit row without re-hashing the chain.
    with ctx.db.connect() as conn:
        conn.execute("UPDATE audit_log SET details='tampered' WHERE action='create' AND entity_type='invoice'")
    broken = verify_audit_chain(ctx.db)
    assert broken["valid"] is False
    assert broken["broken_at"] is not None


def test_backup_create_validate_restore(ctx, seeded, tmp_path):
    supplier_id = ctx.suppliers.list()[0]["id"]
    ctx.invoices.create_invoice(
        invoice_type="purchase", supplier_id=supplier_id, paid_amount=5,
        lines=[InvoiceLineInput(description="A", item_id=seeded["item_id"], quantity=1, unit_price=12)],
    )
    target = tmp_path / "b.nanobackup"
    out = ctx.backup.create_backup(target)
    assert Path(out).exists()
    validation = ctx.backup.validate_backup(out)
    assert validation.valid and int(validation.schema_version) >= 1
    # mutate after backup via a real accounting edit, then restore the file
    ctx.invoices.delete_invoice(
        ctx.invoices.list_invoices()[0]["id"]
    )
    assert ctx.invoices.list_invoices() == []
    ctx.backup.restore_backup(out)
    ctx.reload(ctx.db.path)
    assert len(ctx.invoices.list_invoices()) == 1


def test_initialize_fresh_database_keeps_license_and_creates_safety_backup(ctx, seeded):
    ctx.auth.create_user(
        username="admin",
        full_name="مدير",
        password="password123",
        role="admin",
        _bootstrap=True,
    )
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO license_state(id,license_key,device_id) VALUES(1,'license-key','device-id')"
        )

    safety_path = ctx.backup.initialize_fresh_database()
    ctx.reload(ctx.db.path)

    assert safety_path.is_file()
    assert ctx.backup.validate_backup(safety_path).valid
    assert ctx.items.list() == []
    assert ctx.auth.has_users() is False
    with ctx.db.connect() as conn:
        license_row = conn.execute("SELECT license_key,device_id FROM license_state WHERE id=1").fetchone()
    assert tuple(license_row) == ("license-key", "device-id")
    assert ctx.db.integrity_check().lower() == "ok"


def test_backup_encrypted_requires_password(ctx, seeded, tmp_path):
    target = tmp_path / "enc.nanobackup"
    ctx.backup.create_backup(target, password="s3cret!")
    manifest = ctx.backup.validate_backup(target, password="s3cret!")
    assert manifest.valid and manifest.encrypted is True
    # wrong/absent password must fail on both validate and restore
    with pytest.raises(ValueError):
        ctx.backup.validate_backup(target)
    with pytest.raises(ValueError):
        ctx.backup.restore_backup(target)


def test_delete_opening_balance_clears_baseline_and_survives_rebuild(tmp_path):
    ctx = AppContext.create(tmp_path / "opening.db")
    item_id = ctx.items.create(name="افتتاحي", purchase_price=10, selling_price=15, quantity=7)
    assert float(ctx.items.get(item_id)["opening_quantity"]) == 7
    assert len(ctx.items.movements(item_id)) == 1

    result = ctx.items.delete_opening_balance(item_id)
    assert result["removed_quantity"] == 7

    item = ctx.items.get(item_id)
    assert float(item["quantity"]) == 0
    assert float(item["opening_quantity"]) == 0
    assert float(item["average_cost"]) == 0
    assert ctx.items.movements(item_id) == []

    fresh = AppContext.create(tmp_path / "opening.db")  # restart -> rebuild
    assert float(fresh.items.get(item_id)["quantity"]) == 0
    assert ctx.db.integrity_check().lower() == "ok"


def test_delete_opening_balance_blocked_after_invoice(ctx, seeded):
    supplier_id = ctx.suppliers.list()[0]["id"]
    ctx.invoices.create_invoice(
        invoice_type="purchase", supplier_id=supplier_id, paid_amount=0,
        lines=[InvoiceLineInput(description="A", item_id=seeded["item_id"], quantity=2, unit_price=12)],
    )
    with pytest.raises(ValueError, match="فاتورة"):
        ctx.items.delete_opening_balance(seeded["item_id"])


def test_delete_opening_balance_rejects_services(ctx):
    service_id = ctx.items.create(name="خدمة", item_type="خدمة", purchase_price=0, selling_price=20)
    with pytest.raises(ValueError, match="المخزون"):
        ctx.items.delete_opening_balance(service_id)


def test_opening_invoice_refreshes_after_item_quantity_edit(ctx):
    item_id = ctx.items.create(name="مادة افتتاحية قابلة للتعديل", purchase_price=10, quantity=100)
    other_item_id = ctx.items.create(name="مادة افتتاحية ثابتة", purchase_price=5, quantity=3)
    opening_id = ctx.invoices.convert_opening_balances()
    assert opening_id is not None

    ctx.items.update(
        item_id, name="مادة افتتاحية قابلة للتعديل", purchase_price=10,
        selling_price=15, quantity=120,
    )
    assert float(ctx.items.get(item_id)["quantity"]) == 120

    assert ctx.invoices.convert_opening_balances(item_ids=[item_id]) == opening_id
    with ctx.db.connect() as conn:
        invoice_ids = [row["id"] for row in conn.execute(
            "SELECT DISTINCT invoice_id AS id FROM invoice_lines WHERE item_id=?",
            (item_id,),
        )]
        line = conn.execute(
            "SELECT quantity_in_base FROM invoice_lines WHERE invoice_id=? AND item_id=?",
            (opening_id, item_id),
        ).fetchone()
        other_line = conn.execute(
            "SELECT quantity_in_base FROM invoice_lines WHERE invoice_id=? AND item_id=?",
            (opening_id, other_item_id),
        ).fetchone()
        invoice_total = conn.execute(
            "SELECT total FROM invoices WHERE id=?",
            (opening_id,),
        ).fetchone()[0]
        adjustments = conn.execute(
            "SELECT COUNT(*) FROM inventory_movements WHERE item_id=? AND movement_type='adjustment'",
            (item_id,),
        ).fetchone()[0]
    assert invoice_ids == [opening_id]
    assert float(line["quantity_in_base"]) == 120
    assert float(other_line["quantity_in_base"]) == 3
    assert float(invoice_total) == 1215
    assert adjustments == 0
    assert float(ctx.items.get(item_id)["quantity"]) == 120

    ctx.invoices.delete_invoice(opening_id)
    assert float(ctx.items.get(item_id)["quantity"]) == 0
    assert float(ctx.items.get(other_item_id)["quantity"]) == 0
    ctx.items.delete(item_id)
    ctx.items.delete(other_item_id)
    assert ctx.items.get(item_id) is None


def test_zero_opening_quantity_removes_empty_opening_invoice(ctx):
    item_id = ctx.items.create(name="مادة رصيدها صفر", purchase_price=10, quantity=100)
    opening_id = ctx.invoices.convert_opening_balances(item_ids=[item_id])
    assert opening_id is not None

    ctx.items.update(
        item_id, name="مادة رصيدها صفر", purchase_price=10,
        selling_price=15, quantity=0,
    )
    assert ctx.invoices.convert_opening_balances(item_ids=[item_id]) == opening_id
    assert float(ctx.items.get(item_id)["quantity"]) == 0
    assert ctx.items.movements(item_id) == []
    with ctx.db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE id=?",
            (opening_id,),
        ).fetchone()[0] == 0
    ctx.items.delete(item_id)


def test_opening_invoice_marker_is_preserved_when_editing_notes(ctx):
    item_id = ctx.items.create(name="مادة ملاحظاتها", purchase_price=10, quantity=10)
    opening_id = ctx.invoices.convert_opening_balances(item_ids=[item_id])
    assert opening_id is not None
    with ctx.db.connect() as conn:
        line = conn.execute(
            "SELECT unit_id FROM invoice_lines WHERE invoice_id=? AND item_id=?",
            (opening_id, item_id),
        ).fetchone()
        invoice = conn.execute(
            "SELECT total FROM invoices WHERE id=?",
            (opening_id,),
        ).fetchone()

    ctx.invoices.update_invoice(
        opening_id,
        invoice_type="purchase",
        lines=[InvoiceLineInput(
            description="مادة ملاحظاتها", item_id=item_id, unit_id=line["unit_id"],
            quantity=10, unit_price=10,
        )],
        notes="ملاحظة المستخدم",
        paid_amount=float(invoice["total"]),
    )
    with ctx.db.connect() as conn:
        notes = conn.execute(
            "SELECT notes FROM invoices WHERE id=?",
            (opening_id,),
        ).fetchone()["notes"]
    assert "ملاحظة المستخدم" in notes
    assert "__opening_balance__" in notes

    with pytest.raises(ValueError, match="فاتورة «أول المدة»"):
        ctx.invoices.update_invoice(
            opening_id,
            invoice_type="sale",
            lines=[InvoiceLineInput(
                description="مادة ملاحظاتها", item_id=item_id, unit_id=line["unit_id"],
                quantity=10, unit_price=10,
            )],
            notes="ملاحظة المستخدم",
            paid_amount=float(invoice["total"]),
        )


def test_zero_stock_item_with_adjustment_only_history_can_be_deleted(ctx):
    item_id = ctx.items.create(name="مادة تسويات فقط", purchase_price=10, quantity=100)
    opening_id = ctx.invoices.convert_opening_balances(item_ids=[item_id])
    ctx.items.update(
        item_id, name="مادة تسويات فقط", purchase_price=10,
        selling_price=15, quantity=120,
    )
    ctx.invoices.delete_invoice(opening_id)
    assert float(ctx.items.get(item_id)["quantity"]) == 20
    with pytest.raises(ValueError, match="حركات مخزون"):
        ctx.items.delete(item_id)

    ctx.items.update(
        item_id, name="مادة تسويات فقط", purchase_price=10,
        selling_price=15, quantity=0,
    )
    assert float(ctx.items.get(item_id)["quantity"]) == 0
    assert len(ctx.items.movements(item_id)) == 2
    ctx.items.delete(item_id)
    assert ctx.items.get(item_id) is None
    with ctx.db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM inventory_movements WHERE item_id=?",
            (item_id,),
        ).fetchone()[0] == 0
