from __future__ import annotations

import sqlite3


_EPSILON = 1e-9


def record_inventory_adjustment(
    conn: sqlite3.Connection,
    item_id: int,
    diff: float,
    unit_cost: float,
) -> None:
    """Record a stock adjustment and shift the rebuildable opening baseline.

    The movement is the visible audit trail; shifting the baseline ensures
    inventory rebuilds retain the adjustment before replaying invoice lines.
    """
    if abs(diff) < _EPSILON:
        return

    item = conn.execute(
        "SELECT quantity,opening_quantity,opening_unit_cost FROM items WHERE id=?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise ValueError("المادة غير موجودة")

    today = conn.execute("SELECT date('now') AS d").fetchone()["d"]
    conn.execute(
        """INSERT INTO inventory_movements(item_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date)
           VALUES(?,?,?,?,?,?)""",
        (item_id, "adjustment", diff, unit_cost, diff * unit_cost, today),
    )

    old_opening_qty = float(item["opening_quantity"] or 0)
    old_opening_cost = float(item["opening_unit_cost"] or 0)
    new_opening_qty = old_opening_qty + diff
    new_opening_value = old_opening_qty * old_opening_cost + diff * unit_cost
    new_opening_cost = (
        new_opening_value / new_opening_qty
        if abs(new_opening_qty) > _EPSILON
        else unit_cost
    )
    conn.execute(
        """UPDATE items SET quantity=quantity+?, opening_quantity=?,
               opening_unit_cost=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (diff, new_opening_qty, new_opening_cost, item_id),
    )
