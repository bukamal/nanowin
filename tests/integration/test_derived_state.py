from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core import derived_state
from nano_offline.services.invoice_service import InvoiceLineInput


def test_clean_marker_skips_and_raw_change_forces_rebuild():
    tmp = tempfile.mkdtemp(prefix="nano-derived-")
    db_path = Path(tmp) / "nano.db"
    try:
        ctx = AppContext.create(db_path)
        ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
        item = ctx.items.create(name="مادة", selling_price=2.0, purchase_price=1.0, quantity=5)
        ctx.invoices.create_invoice(
            invoice_type="sale", paid_amount=0,
            lines=[InvoiceLineInput(description="x", item_id=int(item), quantity=1, unit_price=2.0)],
        )
        # Any service mutation leaves derived state clean.
        assert derived_state.is_clean(ctx.db) is True

        # A raw change (bypassing the services) invalidates the marker.
        with ctx.db.transaction() as conn:
            conn.execute(
                "INSERT INTO invoices(type,invoice_date,total,initial_paid_amount,paid_amount) "
                "VALUES('sale','2026-01-01',1,0,0)"
            )
        assert derived_state.is_clean(ctx.db) is False

        # Re-opening rebuilds once, then the marker is clean again.
        ctx2 = AppContext.create(db_path)
        assert derived_state.is_clean(ctx2.db) is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pos_catalog_is_cheap_and_best_sellers_are_cached():
    tmp = tempfile.mkdtemp(prefix="nano-pos-catalog-")
    try:
        ctx = AppContext.create(Path(tmp) / "nano.db")
        ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
        item = ctx.items.create(name="مادة سريعة", selling_price=2.0, purchase_price=1.0, quantity=50)
        ctx.invoices.create_invoice(
            invoice_type="sale", paid_amount=0,
            lines=[InvoiceLineInput(description="x", item_id=int(item), quantity=3, unit_price=2.0)],
        )

        catalog = ctx.items.pos_catalog()
        assert catalog and all(float(it.get("sold_qty") or 0) == 0 for it in catalog)

        ctx.items.refresh_best_sellers(5)
        best = ctx.items.best_sellers(5)
        assert best and int(best[0]["id"]) == int(item)
        assert float(best[0]["sold_qty"]) >= 3
        assert ctx.items.best_sellers_stale(max_age_seconds=10 ** 6) is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
