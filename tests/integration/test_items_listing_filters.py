"""Server-side listing for the Materials screen: chip-filter modes, summaries,
sorting and paging all answered by one indexed SQL query per refresh.

These guard the contract the screen relies on after the performance refactor:
``list``/``count``/``summary`` must agree on the same ``mode``/``search`` so a
page never shows rows the total disagrees with.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from nano_offline.core.fsutil import clean_tmpdir
from nano_offline.app_context import AppContext


def _ctx():
    tmp = tempfile.TemporaryDirectory(prefix="nano-items-listing-")
    return AppContext.create(Path(tmp.name) / "nano.db"), tmp


def _seed(ctx) -> dict[str, int]:
    a = ctx.items.create(name="A حبر", barcode="111", quantity=10, purchase_price=2, selling_price=5)
    b = ctx.items.create(name="B ورق", barcode="222", quantity=3, purchase_price=1, selling_price=4)
    c = ctx.items.create(name="C خدمة", item_type="خدمة", selling_price=100)
    d = ctx.items.create(
        name="D تجميعية",
        is_composite=True,
        components=[{"component_item_id": a, "quantity": 1}],
    )
    e = ctx.items.create(name="E بلا شيء", quantity=0, purchase_price=0, selling_price=0)
    return {"a": a, "b": b, "c": c, "d": d, "e": e}


def test_list_modes_filter_the_right_rows():
    ctx, tmp = _ctx()
    try:
        ids = _seed(ctx)
        def names(mode, **kw):
            return {r["name"] for r in ctx.items.list(mode=mode, **kw)}

        assert len(ctx.items.list()) == 5
        assert names("all") == {"A حبر", "B ورق", "C خدمة", "D تجميعية", "E بلا شيء"}
        assert names("stock") == {"A حبر", "B ورق", "E بلا شيء"}
        assert names("composite") == {"D تجميعية"}
        assert names("service") == {"C خدمة"}
        assert names("low", low_stock=5.0) == {"B ورق", "E بلا شيء"}
        assert names("no_price") == {"E بلا شيء"}
        # no_barcode is type-agnostic: the service and the composite have none.
        assert names("no_barcode") == {"C خدمة", "D تجميعية", "E بلا شيء"}
        # restock is driven by an explicit id set from the prediction service.
        assert names("restock", ids=[ids["a"], ids["b"]]) == {"A حبر", "B ورق"}
        assert names("restock", ids=[]) == set()
    finally:
        clean_tmpdir(tmp)


def test_list_and_count_agree_for_every_mode():
    ctx, tmp = _ctx()
    try:
        _seed(ctx)
        for mode in ("all", "stock", "composite", "service", "low", "no_barcode", "no_price"):
            rows = ctx.items.list(mode=mode)
            assert ctx.items.count(mode=mode) == len(rows), mode
        # Search combines with the chip filter instead of replacing it.
        rows = ctx.items.list(search="B", mode="stock")
        assert [r["name"] for r in rows] == ["B ورق"]
        assert ctx.items.count(search="B", mode="stock") == 1
        # A search that matches nothing while a filter is active stays empty.
        assert ctx.items.list(search="zzz", mode="low") == []
        assert ctx.items.count(search="zzz", mode="low") == 0
    finally:
        clean_tmpdir(tmp)


def test_summary_aggregates_a_single_query():
    ctx, tmp = _ctx()
    try:
        _seed(ctx)
        s = ctx.items.summary()
        assert s["count"] == 5
        assert s["total_quantity"] == 13.0          # 10 + 3 + 0 (composite/service excluded)
        assert s["inventory_value"] == 23.0         # 10*2 + 3*1
        assert s["low_count"] == 2                  # B (3) + E (0)
        assert s["service_count"] == 1

        # The same call scoped to a chip filter drives the footer totals.
        low = ctx.items.summary(mode="low", low_stock=5.0)
        assert low["count"] == 2
        assert low["total_quantity"] == 3.0         # B only (E has 0)
        assert low["inventory_value"] == 3.0        # 3*1

        # A custom threshold moves the cutoff without a code change.
        strict = ctx.items.summary(low_stock=11.0)
        assert strict["low_count"] == 3             # A moved into the low bucket
    finally:
        clean_tmpdir(tmp)


def test_list_sorting_and_paging():
    ctx, tmp = _ctx()
    try:
        _seed(ctx)
        # Preset sorts (dropdown) and header sorts (DataGrid) share the query.
        assert ctx.items.list(order_by="price_desc")[0]["name"] == "C خدمة"
        asc_prices = [float(r["selling_price"] or 0) for r in ctx.items.list(order_by="price_asc")]
        assert asc_prices == sorted(asc_prices)
        qty_order = [r["name"] for r in ctx.items.list(order_by="qty_asc")]
        assert qty_order[-1] == "C خدمة"            # services pushed to the end
        header = [r["name"] for r in ctx.items.list(order_by="selling_price", descending=True)]
        assert header[0] == "C خدمة"

        # Paging tiles the (stable) ordered set without gaps or overlaps.
        seen: list[int] = []
        for offset in range(0, 5, 2):
            page = ctx.items.list(limit=2, offset=offset)
            seen.extend(r["id"] for r in page)
        assert len(seen) == len(set(seen)) == 5
        assert seen == [r["id"] for r in ctx.items.list()]
    finally:
        clean_tmpdir(tmp)
