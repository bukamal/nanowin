"""Item edit data path: secondary unit + barcode round-trip and re-save."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from nano_offline.app_context import AppContext


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-item-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def test_secondary_unit_and_barcode_survive_edit():
    ctx, tmp = _ctx()
    try:
        base = ctx.definitions.create_unit("كيلو", "كغ")
        sack = ctx.definitions.create_unit("شوال", "ش")
        item = ctx.items.create(
            name="سكر", purchase_price=10.0, selling_price=15.0, quantity=0.0,
            base_unit_id=base, barcode="SUGAR-1",
            item_units=[{"unit_id": sack, "conversion_factor": 50, "purchase_price": 11000.0, "selling_price": 16000.0}],
        )
        ctx.items.add_barcode(item, "SACK-1", unit_id=sack)

        units = ctx.items.units(item)
        assert units[0]["is_base"] is True
        alt = next((u for u in units if not u.get("is_base")), None)
        assert alt is not None and int(alt["id"]) == int(sack)
        assert float(alt["conversion_factor"]) == 50.0
        assert float(alt["purchase_price"]) == 11000.0

        barcodes = ctx.items.list_barcodes(item)
        assert any(b["barcode"] == "SACK-1" and int(b["unit_id"]) == int(sack) for b in barcodes)

        # Re-saving the edit with the SAME primary barcode must not fail.
        ctx.items.update(
            item, name="سكر أبيض", barcode="SUGAR-1", base_unit_id=base,
            item_units=[{"unit_id": sack, "conversion_factor": 50, "purchase_price": 11000.0, "selling_price": 16000.0}],
        )
        assert ctx.items.get(item)["name"] == "سكر أبيض"
        again = ctx.items.units(item)
        alt2 = next((u for u in again if not u.get("is_base")), None)
        assert alt2 is not None and float(alt2["conversion_factor"]) == 50.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
