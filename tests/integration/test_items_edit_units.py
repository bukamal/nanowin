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


def test_multiple_additional_units_with_prices_and_barcodes():
    ctx, tmp = _ctx()
    try:
        base = ctx.definitions.create_unit("حبة", "ح")
        pack = ctx.definitions.create_unit("علبة", "ع")
        carton = ctx.definitions.create_unit("كرتون", "ك")
        item = ctx.items.create(
            name="ماء", purchase_price=1.0, selling_price=2.0, base_unit_id=base,
            item_units=[
                {"unit_id": pack, "conversion_factor": 12, "purchase_price": 10.0, "selling_price": 18.0},
                {"unit_id": carton, "conversion_factor": 144, "purchase_price": 110.0, "selling_price": 200.0},
            ],
        )
        ctx.items.add_barcode(item, "PACK-1", unit_id=pack)
        ctx.items.add_barcode(item, "CARTON-1", unit_id=carton)

        units = {int(u["id"]): u for u in ctx.items.units(item) if not u.get("is_base")}
        assert float(units[pack]["conversion_factor"]) == 12.0
        assert float(units[pack]["selling_price"]) == 18.0
        assert float(units[carton]["conversion_factor"]) == 144.0
        assert float(units[carton]["purchase_price"]) == 110.0

        codes = {int(b["unit_id"]): b["barcode"] for b in ctx.items.list_barcodes(item)}
        assert codes[pack] == "PACK-1"
        assert codes[carton] == "CARTON-1"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
