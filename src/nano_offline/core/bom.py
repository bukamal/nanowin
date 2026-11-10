"""Bill-of-materials helpers for composite/assembly items.

A composite item (``items.is_composite=1``) is a sellable material built from
other stock materials listed in ``item_components``. It never holds stock
itself: selling it explodes the recipe down to its **leaf** stock components
(recursively, so a component may itself be composite) and consumes those.

Pure ``sqlite3.Connection`` functions with no service/repository dependency,
so both ``ItemRepository`` (write/validate) and ``InvoiceService`` (sale
explosion) can share the exact same logic.
"""

from __future__ import annotations

import sqlite3

__all__ = ["explode", "assert_acyclic", "recipe_rows"]


def recipe_rows(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """Direct component rows of ``item_id`` (one level, not exploded)."""
    return conn.execute(
        "SELECT component_item_id, quantity, conversion_factor FROM item_components "
        "WHERE item_id=? ORDER BY id",
        (int(item_id),),
    ).fetchall()


def explode(conn: sqlite3.Connection, item_id: int, multiplier: float) -> dict[int, float]:
    """Return ``{leaf_component_item_id: base_quantity}`` for ``multiplier``
    base units of composite ``item_id``.

    Raises ``ValueError`` with an Arabic message when the recipe is empty, a
    non-stock material is used, or the recipe contains a cycle.
    """
    acc: dict[int, float] = {}
    _explode(conn, int(item_id), float(multiplier), acc, set())
    return acc


def _explode(
    conn: sqlite3.Connection,
    item_id: int,
    multiplier: float,
    acc: dict[int, float],
    stack: set[int],
) -> None:
    if item_id in stack:
        raise ValueError("وصفة التصنيع تحتوي على دورة — مادة تتضمن نفسها بشكل غير مباشر")
    rows = recipe_rows(conn, item_id)
    if not rows:
        name = _item_name(conn, item_id)
        raise ValueError(f"المادة التجميعية «{name}» لا تحتوي على مواد أولية")
    stack.add(item_id)
    try:
        for row in rows:
            component_id = int(row["component_item_id"])
            per_unit = float(row["quantity"] or 0) * float(row["conversion_factor"] or 1)
            if per_unit <= 0:
                raise ValueError("كمية أحد مكوّنات وصفة التصنيع غير صحيحة")
            component = conn.execute(
                "SELECT item_type,is_composite,name FROM items WHERE id=?", (component_id,)
            ).fetchone()
            if component is None:
                raise ValueError("أحد مكوّنات وصفة التصنيع غير موجود")
            quantity = per_unit * multiplier
            if int(component["is_composite"] or 0):
                _explode(conn, component_id, quantity, acc, stack)
            else:
                if str(component["item_type"]) != "مخزون":
                    raise ValueError(
                        f"مكوّن الوصفة «{component['name']}» يجب أن يكون مادة مخزون لا خدمة"
                    )
                acc[component_id] = acc.get(component_id, 0.0) + quantity
    finally:
        stack.discard(item_id)


def assert_acyclic(conn: sqlite3.Connection, root_item_id: int) -> None:
    """Raise ``ValueError`` if the stored recipe reachable from
    ``root_item_id`` contains a cycle."""
    root = int(root_item_id)
    visiting: set[int] = set()
    done: set[int] = set()

    def dfs(node: int) -> None:
        if node in done:
            return
        if node in visiting:
            raise ValueError("وصفة التصنيع تحتوي على دورة — مادة تتضمن نفسها")
        visiting.add(node)
        for row in recipe_rows(conn, node):
            dfs(int(row["component_item_id"]))
        visiting.discard(node)
        done.add(node)

    dfs(root)


def _item_name(conn: sqlite3.Connection, item_id: int) -> str:
    row = conn.execute("SELECT name FROM items WHERE id=?", (int(item_id),)).fetchone()
    return str(row["name"]) if row is not None else str(item_id)