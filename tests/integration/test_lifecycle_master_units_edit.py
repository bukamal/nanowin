"""Extended lifecycle: item master with sub-units, per-unit barcodes/prices,
opening quantities and a full edit -- then the accounting invariants."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import invariants  # noqa: E402

from nano_offline.app_context import AppContext  # noqa: E402


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-master-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def _unit_by_name(ctx, name: str) -> dict:
    return next(u for u in ctx.definitions.list_units() if u["name"] == name)


def test_item_subunits_barcodes_prices_and_full_edit():
    ctx, tmp = _ctx()
    try:
        kg = ctx.definitions.create_unit("كيلو", "كغ")
        sack = ctx.definitions.create_unit("شوال", "ش")
        box = ctx.definitions.create_unit("علبة", "ع")
        category = ctx.definitions.create_category("مواد غذائية")

        item = ctx.items.create(
            name="سكر", category_id=category, item_type="مخزون",
            purchase_price=0.9, selling_price=1.2, quantity=0.0,
            base_unit_id=kg, barcode="SUGAR-KG",
            item_units=[
                {"unit_id": sack, "conversion_factor": 50, "purchase_price": 45.0, "selling_price": 60.0},
                {"unit_id": box, "conversion_factor": 10, "purchase_price": 9.0, "selling_price": 12.0},
            ],
        )
        ctx.items.add_barcode(item, "SUGAR-SACK", unit_id=sack)
        ctx.items.add_barcode(item, "SUGAR-BOX", unit_id=box)

        # --- sub-units carry their own factor + prices --------------------- #
        units = ctx.items.units(item)
        assert units[0]["is_base"] is True and int(units[0]["id"]) == int(kg)
        by_id = {int(u["id"]): u for u in units}
        assert float(by_id[sack]["conversion_factor"]) == 50.0
        assert float(by_id[sack]["purchase_price"]) == 45.0
        assert float(by_id[sack]["selling_price"]) == 60.0
        assert float(by_id[box]["conversion_factor"]) == 10.0

        # --- barcodes resolve to the item + their unit --------------------- #
        assert ctx.items.find_by_barcode("SUGAR-KG")["id"] == item
        sack_hit = ctx.items.find_by_barcode("SUGAR-SACK")
        assert sack_hit["id"] == item and int(sack_hit["matched_unit_id"]) == int(sack)
        box_hit = ctx.items.find_by_barcode("SUGAR-BOX")
        assert int(box_hit["matched_unit_id"]) == int(box)

        # --- full edit: rename, retune prices, swap a sub-unit ------------- #
        carton = ctx.definitions.create_unit("كرتونة", "ك")
        ctx.items.update(
            item, name="سكر فاخر", category_id=category, item_type="مخزون",
            purchase_price=1.0, selling_price=1.4, base_unit_id=kg, barcode="SUGAR-KG",
            item_units=[
                {"unit_id": sack, "conversion_factor": 50, "purchase_price": 48.0, "selling_price": 66.0},
                {"unit_id": carton, "conversion_factor": 24, "purchase_price": 22.0, "selling_price": 30.0},
            ],
        )
        ctx.items.add_barcode(item, "SUGAR-CTN", unit_id=carton)

        after = ctx.items.get(item)
        assert after["name"] == "سكر فاخر"
        assert float(after["purchase_price"]) == 1.0
        ids = {int(u["id"]) for u in ctx.items.units(item) if not u.get("is_base")}
        assert ids == {int(sack), int(carton)}  # box removed, carton added
        assert int(ctx.items.find_by_barcode("SUGAR-CTN")["matched_unit_id"]) == int(carton)

        # Re-adding an identical secondary code is a clear (handled) error.
        with pytest.raises(ValueError):
            ctx.items.add_barcode(item, "SUGAR-CTN", unit_id=carton)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_opening_quantities_and_invariants():
    ctx, tmp = _ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        item = ctx.items.create(
            name="رز", purchase_price=2.0, selling_price=3.0, quantity=20.0,
            base_unit_id=unit, barcode="RICE-1",
        )
        opening = ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
        assert opening is not None
        row = ctx.items.get(item)
        assert float(row["quantity"]) == 20.0
        assert float(row["average_cost"]) == 2.0
        assert ctx.reports.inventory_valuation()["total_value"] == pytest.approx(40.0, abs=1e-4)

        invariants.assert_all(ctx)
        invariants.assert_rebuild_idempotent(ctx)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)