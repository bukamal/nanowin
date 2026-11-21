from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.services.invoice_service import InvoiceLineInput


def test_totals_and_paging_cover_the_whole_table():
    tmp = tempfile.mkdtemp(prefix="nano-inv-page-")
    try:
        ctx = AppContext.create(Path(tmp) / "nano.db")
        ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
        item = ctx.items.create(name="مادة", selling_price=2.0, purchase_price=1.0, quantity=1000)
        for _ in range(7):
            ctx.invoices.create_invoice(
                invoice_type="sale", paid_amount=0,
                lines=[InvoiceLineInput(description="x", item_id=int(item), quantity=1, unit_price=2.0)],
            )
        for _ in range(3):
            ctx.invoices.create_invoice(
                invoice_type="purchase", paid_amount=0,
                lines=[InvoiceLineInput(description="x", item_id=int(item), quantity=1, unit_price=1.0)],
            )

        assert ctx.invoices.count_invoices() == 10
        assert ctx.invoices.count_invoices("sale") == 7
        totals = ctx.invoices.invoice_totals()
        assert totals["count"] == 10
        assert round(totals["sales_sum"], 2) == 14.0
        assert round(totals["purchases_sum"], 2) == 3.0
        # Profit = sale total - COGS (COGS uses moving average ~1.0/unit here).
        assert totals["profit_sum"] > 0

        first = ctx.invoices.list_invoices(limit=4, offset=0)
        second = ctx.invoices.list_invoices(limit=4, offset=4)
        third = ctx.invoices.list_invoices(limit=4, offset=8)
        assert len(first) == 4 and len(second) == 4 and len(third) == 2
        ids = {row["id"] for row in first + second + third}
        assert len(ids) == 10
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
