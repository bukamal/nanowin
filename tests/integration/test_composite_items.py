"""Composite/assembly item lifecycle (schema 18).

A composite is a sellable material built from other stock materials. It never
holds stock itself: selling it explodes the recipe down to leaf components and
consumes those from inventory, while the sale line freezes the exploded
components so later recipe edits cannot rewrite history.
"""

from __future__ import annotations

import pytest

from nano_offline.app_context import AppContext
from nano_offline.services.invoice_service import InvoiceLineInput


def _ctx(tmp_path) -> AppContext:
    return AppContext.create(tmp_path / "nano.db")


def _stock_item(ctx: AppContext, name: str, qty: float, cost: float) -> int:
    return ctx.items.create(
        name=name, item_type="مخزون", purchase_price=cost, selling_price=cost * 2, quantity=qty
    )


def _composite(ctx: AppContext, name: str, price: float, components: list[tuple[int, float]]) -> int:
    return ctx.items.create(
        name=name,
        item_type="مخزون",
        selling_price=price,
        is_composite=True,
        components=[
            {"component_item_id": cid, "quantity": qty, "conversion_factor": 1}
            for cid, qty in components
        ],
    )


def _sell(ctx: AppContext, item_id: int, qty: float, price: float, *, allow_negative: bool = False) -> int:
    return ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="x", item_id=item_id, quantity=qty, unit_price=price)],
        paid_amount=qty * price,
        allow_negative_stock=allow_negative,
    )


def _qty(ctx: AppContext, item_id: int) -> float:
    return float(ctx.items.get(item_id)["quantity"])


def test_composite_sale_consumes_components_and_has_no_own_stock(tmp_path):
    ctx = _ctx(tmp_path)
    flour = _stock_item(ctx, "طحين", 100, 2)
    sugar = _stock_item(ctx, "سكر", 100, 5)
    cake = _composite(ctx, "كعكة", 20, [(flour, 2), (sugar, 1)])

    invoice_id = _sell(ctx, cake, 3, 20)

    assert _qty(ctx, flour) == pytest.approx(94)   # 100 - 3*2
    assert _qty(ctx, sugar) == pytest.approx(97)   # 100 - 3*1
    assert _qty(ctx, cake) == pytest.approx(0)      # composites hold no stock

    line = ctx.invoices.get_invoice(invoice_id)["lines"][0]
    # 3 cakes = 6 flour @2 (=12) + 3 sugar @5 (=15) -> 27 total, 9 per cake
    assert line["cost_amount"] == pytest.approx(27)
    assert line["unit_cost"] == pytest.approx(9)


def test_composite_multi_level_explodes_to_leaf_materials(tmp_path):
    ctx = _ctx(tmp_path)
    leaf = _stock_item(ctx, "مادة أساسية", 100, 1)
    sub = _composite(ctx, "نصف مصنّع", 6, [(leaf, 2)])
    top = _composite(ctx, "منتج نهائي", 30, [(sub, 3)])

    _sell(ctx, top, 2, 30)

    # 2 top x 3 sub x 2 leaf = 12 leaves consumed
    assert _qty(ctx, leaf) == pytest.approx(88)


def test_composite_oversell_blocked_unless_allowed(tmp_path):
    ctx = _ctx(tmp_path)
    leaf = _stock_item(ctx, "مكوّن", 5, 1)
    bundle = _composite(ctx, "طقم", 10, [(leaf, 3)])

    with pytest.raises(ValueError):
        _sell(ctx, bundle, 2, 10)  # needs 6, only 5 in stock

    _sell(ctx, bundle, 2, 10, allow_negative=True)
    assert _qty(ctx, leaf) == pytest.approx(-1)


def test_editing_recipe_does_not_rewrite_historical_consumption(tmp_path):
    ctx = _ctx(tmp_path)
    a = _stock_item(ctx, "أ", 100, 1)
    b = _stock_item(ctx, "ب", 100, 1)
    combo = _composite(ctx, "مركّب", 10, [(a, 2)])

    _sell(ctx, combo, 1, 10)
    assert _qty(ctx, a) == pytest.approx(98)

    # Change the recipe to consume B instead; the already-issued invoice must
    # keep consuming A thanks to its frozen per-line snapshot.
    ctx.items.update(
        combo,
        name="مركّب",
        item_type="مخزون",
        selling_price=10,
        is_composite=True,
        components=[{"component_item_id": b, "quantity": 5, "conversion_factor": 1}],
    )
    ctx.invoices.rebuild_derived_state()

    assert _qty(ctx, a) == pytest.approx(98)
    assert _qty(ctx, b) == pytest.approx(100)

    _sell(ctx, combo, 1, 10)
    assert _qty(ctx, a) == pytest.approx(98)
    assert _qty(ctx, b) == pytest.approx(95)


def test_composite_recipe_cycle_is_rejected(tmp_path):
    ctx = _ctx(tmp_path)
    a = _composite(ctx, "أ", 10, [(_stock_item(ctx, "م1", 10, 1), 1)])
    b = _composite(ctx, "ب", 10, [(_stock_item(ctx, "م2", 10, 1), 1)])

    # a -> b is fine (still acyclic), but then b -> a closes a cycle.
    ctx.items.update(
        a,
        name="أ",
        item_type="مخزون",
        selling_price=10,
        is_composite=True,
        components=[{"component_item_id": b, "quantity": 1, "conversion_factor": 1}],
    )
    with pytest.raises(ValueError):
        ctx.items.update(
            b,
            name="ب",
            item_type="مخزون",
            selling_price=10,
            is_composite=True,
            components=[{"component_item_id": a, "quantity": 1, "conversion_factor": 1}],
        )

    # b's rejected edit must not have partially applied: its recipe stays م2.
    names = {str(r["component_name"]) for r in ctx.items.components(b)}
    assert names == {"م2"}


def test_composite_cannot_be_purchased(tmp_path):
    ctx = _ctx(tmp_path)
    leaf = _stock_item(ctx, "مكوّن", 10, 1)
    bundle = _composite(ctx, "طقم", 10, [(leaf, 1)])

    with pytest.raises(ValueError):
        ctx.invoices.create_invoice(
            invoice_type="purchase",
            lines=[InvoiceLineInput(description="طقم", item_id=bundle, quantity=1, unit_price=5)],
            paid_amount=5,
        )


def test_composite_rebuild_is_idempotent(tmp_path):
    ctx = _ctx(tmp_path)
    leaf = _stock_item(ctx, "مكوّن", 50, 2)
    bundle = _composite(ctx, "طقم", 10, [(leaf, 2)])
    _sell(ctx, bundle, 4, 10)

    before_leaf = _qty(ctx, leaf)
    before_bundle = _qty(ctx, bundle)
    invoice = ctx.invoices.get_invoice(
        ctx.db.connect().execute("SELECT MAX(id) AS id FROM invoices").fetchone()["id"]
    )
    before_cost = invoice["lines"][0]["cost_amount"]

    ctx.invoices.rebuild_derived_state()
    ctx.invoices.rebuild_derived_state()

    assert _qty(ctx, leaf) == pytest.approx(before_leaf)
    assert _qty(ctx, bundle) == pytest.approx(before_bundle)
    invoice = ctx.invoices.get_invoice(invoice["id"])
    assert invoice["lines"][0]["cost_amount"] == pytest.approx(before_cost)