"""Regression: a manually set purchase price must survive the rebuild.

``AppContext.create`` runs ``AccountingRebuilder.rebuild`` on every start,
which recomputes each stock item's quantity/average_cost (and seeds
purchase_price from the opening cost). Before the fix, editing an item's
purchase price did not update the opening-cost baseline, so the next rebuild
reverted purchase_price/average_cost to the original opening cost -- the price
the user saw "changed/decreased" after adding or reopening the app.
"""
from __future__ import annotations

import pytest

from nano_offline.app_context import AppContext


def _make_item(ctx, unit_id):
    return ctx.items.create(
        name="مادة",
        purchase_price=100,
        selling_price=150,
        quantity=10,
        base_unit_id=unit_id,
    )


def test_created_purchase_price_survives_rebuild(tmp_path):
    ctx = AppContext.create(tmp_path / "nano.db")
    unit = ctx.definitions.create_unit("قطعة", "ق")
    item_id = _make_item(ctx, unit)

    fresh = AppContext.create(ctx.db.path)  # simulate a restart -> rebuild
    row = fresh.items.get(item_id)
    assert row is not None
    assert float(row["purchase_price"]) == 100
    assert float(row["average_cost"]) == 100
    assert float(row["quantity"]) == 10


def test_edited_purchase_price_survives_rebuild(tmp_path):
    ctx = AppContext.create(tmp_path / "nano.db")
    unit = ctx.definitions.create_unit("قطعة", "ق")
    item_id = _make_item(ctx, unit)

    ctx.items.update(
        item_id,
        name="مادة",
        category_id=None,
        item_type="مخزون",
        purchase_price=250,
        selling_price=150,
        base_unit_id=unit,
        item_units=[],
        barcode=None,
    )

    fresh = AppContext.create(ctx.db.path)  # restart -> rebuild
    row = fresh.items.get(item_id)
    assert row is not None
    assert float(row["purchase_price"]) == 250
    assert float(row["average_cost"]) == 250
    assert float(row["quantity"]) == 10


def test_editing_zero_quantity_item_adds_rebuildable_stock_adjustment(tmp_path):
    ctx = AppContext.create(tmp_path / "nano.db")
    unit = ctx.definitions.create_unit("قطعة", "ق")
    item_id = ctx.items.create(
        name="مادة بلا مخزون",
        purchase_price=25,
        selling_price=40,
        quantity=0,
        base_unit_id=unit,
    )

    ctx.items.update(
        item_id,
        name="مادة بلا مخزون",
        purchase_price=25,
        selling_price=40,
        base_unit_id=unit,
        quantity=5,
    )
    row = ctx.items.get(item_id)
    assert row is not None
    assert float(row["quantity"]) == 5
    assert float(row["opening_quantity"]) == 5
    assert float(row["average_cost"]) == 25
    adjustments = [m for m in ctx.items.movements(item_id) if m["movement_type"] == "adjustment"]
    assert len(adjustments) == 1
    assert float(adjustments[0]["quantity_delta"]) == 5

    ctx.items.update(
        item_id,
        name="مادة بلا مخزون",
        purchase_price=25,
        selling_price=40,
        base_unit_id=unit,
        quantity=5,
    )
    assert len([m for m in ctx.items.movements(item_id) if m["movement_type"] == "adjustment"]) == 1

    reopened = AppContext.create(ctx.db.path)
    refreshed = reopened.items.get(item_id)
    assert refreshed is not None
    assert float(refreshed["quantity"]) == pytest.approx(5)
