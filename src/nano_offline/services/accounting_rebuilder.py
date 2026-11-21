from __future__ import annotations

from datetime import date

from nano_offline.core import money
from nano_offline.core.cancellation import check_cancel
from nano_offline.core import derived_state

EPSILON = 1e-9

#: Equity account that absorbs party opening balances so they are two-sided
#: (AR/AP vs equity) without touching cash or the income statement.
OPENING_EQUITY_ACCOUNT = "OPENING_EQUITY"

#: Marker stored on an invoice's ``notes`` when it represents a converted
#: opening-stock balance ("أول المدة"). Such invoices exist to expose opening
#: stock in the purchases list and to seed inventory valuation, but they must
#: NOT post cash, payables or period profit (the stock was not bought in this
#: period and no supplier is owed). The rebuilder therefore treats them as
#: inventory-only.
OPENING_BALANCE_MARKER = "__opening_balance__"


def _is_opening_invoice(inv) -> bool:
    try:
        return OPENING_BALANCE_MARKER in str(inv["notes"] or "")
    except Exception:
        return False


class AccountingRebuilder:
    """Rebuild deterministic derived accounting/inventory state.

    Source documents are invoices, vouchers/payments, allocations, expenses and
    explicit opening inventory. Party balances, invoice paid amounts, invoice
    COGS, invoice inventory movements and invoice/payment ledger entries are
    derived state and may be rebuilt safely inside the caller transaction.
    """

    @classmethod
    def rebuild(cls, conn, *, inventory: bool = True, progress=None, cancel=None) -> None:
        if inventory:
            cls.rebuild_inventory(conn, progress=progress, cancel=cancel)
        cls.rebuild_financials(conn, progress=progress, cancel=cancel)
        # Derived state is now current: stamp the marker so the next app launch
        # can skip the (expensive) rebuild entirely.
        derived_state.mark_clean_conn(conn)

    @staticmethod
    def rebuild_inventory(conn, progress=None, cancel=None) -> None:
        conn.execute("DELETE FROM inventory_movements WHERE invoice_id IS NOT NULL")
        conn.execute("""UPDATE invoice_lines SET unit_cost=0,cost_amount=0
                       WHERE item_id IN (SELECT id FROM items WHERE item_type='مخزون')""")

        state: dict[int, dict[str, float]] = {}
        for item in conn.execute(
            "SELECT id,opening_quantity,opening_unit_cost FROM items WHERE item_type='مخزون'"
        ).fetchall():
            qty = float(item["opening_quantity"] or 0)
            # A negative baseline (e.g. a stocktake shrink shifted opening
            # quantity below zero) still carries a cost: using it keeps the
            # baseline's value correct. Zeroing it made every rebuild inflate
            # average_cost/COGS and disagree with the dashboard valuation.
            avg = float(item["opening_unit_cost"] or 0) if abs(qty) > EPSILON else 0.0
            state[int(item["id"])] = {"qty": qty, "avg": avg}
            conn.execute(
                # Preserve a manually set purchase_price (only seed it from the
                # opening cost when none was stored); otherwise a rebuild would
                # revert the user's edit back to the original opening cost.
                "UPDATE items SET quantity=?,average_cost=?,"
                "purchase_price=CASE WHEN purchase_price > 0 THEN purchase_price WHEN ? > 0 THEN ? ELSE purchase_price END "
                "WHERE id=?",
                (qty, avg, qty, avg, item["id"]),
            )

        def get_state(item_id: int) -> dict[str, float]:
            if item_id not in state:
                state[item_id] = {"qty": 0.0, "avg": 0.0}
            return state[item_id]

        rows = conn.execute(
            """
            SELECT il.*, inv.type AS invoice_type, inv.invoice_date,
                   inv.created_at AS invoice_created_at,
                   inv.allow_negative_stock AS allow_negative_stock,
                   it.name AS item_name, it.item_type, it.is_composite AS is_composite
            FROM invoice_lines il
            JOIN invoices inv ON inv.id=il.invoice_id
            LEFT JOIN items it ON it.id=il.item_id
            WHERE il.item_id IS NOT NULL AND it.item_type='مخزون'
            ORDER BY inv.invoice_date, inv.created_at, inv.id, il.id
            """
        ).fetchall()
        total_rows = len(rows)
        if progress:
            progress("rebuild_inventory", 0, float(total_rows or 1), "lines", "إعادة بناء حركات المخزون")

        for index, raw in enumerate(rows, start=1):
            check_cancel(cancel)
            if progress and index % 2000 == 0:
                progress("rebuild_inventory", index, float(total_rows), "lines", "إعادة بناء حركات المخزون")
            row = dict(raw)
            item_id = int(row["item_id"])
            base_qty = float(row["quantity_in_base"])
            line_total = float(row["total"])
            if row["invoice_type"] == "purchase":
                if int(row.get("is_composite") or 0):
                    # Composites are recipes, never purchasable (creation
                    # blocks it). Skip defensively so a rebuild can never
                    # invent stock for one.
                    continue
                st = get_state(item_id)
                unit_cost = line_total / base_qty if base_qty else 0.0
                new_qty = st["qty"] + base_qty
                if new_qty > EPSILON:
                    st["avg"] = ((st["qty"] * st["avg"]) + line_total) / new_qty
                else:
                    # A purchase that only reduces a negative balance (or
                    # lands exactly on zero) has no positive quantity to
                    # spread its value over; use its own unit cost instead
                    # of dividing by a non-positive quantity.
                    st["avg"] = unit_cost
                st["qty"] = new_qty
                conn.execute(
                    "UPDATE invoice_lines SET unit_cost=?,cost_amount=0 WHERE id=?",
                    (unit_cost, row["id"]),
                )
                conn.execute(
                    """INSERT INTO inventory_movements(
                           item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, row["invoice_id"], "purchase", base_qty, unit_cost, line_total, row["invoice_date"]),
                )
                conn.execute("UPDATE items SET purchase_price=? WHERE id=?", (unit_cost, item_id))
            elif int(row.get("is_composite") or 0):
                # Composite sale: the composite itself holds no stock. Its sale
                # line carries a frozen snapshot of the leaf materials to
                # consume; deduct each at its own moving average right here so
                # the cost reflects the timeline position of this invoice.
                allow_negative = bool(row.get("allow_negative_stock"))
                components = conn.execute(
                    "SELECT component_item_id,base_qty FROM invoice_line_components "
                    "WHERE invoice_line_id=? ORDER BY id",
                    (row["id"],),
                ).fetchall()
                if not components:
                    raise ValueError(
                        f"المادة التجميعية «{row['item_name']}» بلا مكوّنات محفوظة في الفاتورة"
                    )
                total_cost = 0.0
                for comp in components:
                    comp_id = int(comp["component_item_id"])
                    comp_qty = float(comp["base_qty"])
                    comp_state = get_state(comp_id)
                    if not allow_negative and comp_state["qty"] + EPSILON < comp_qty:
                        comp_name = conn.execute(
                            "SELECT name FROM items WHERE id=?", (comp_id,)
                        ).fetchone()
                        comp_label = comp_name["name"] if comp_name is not None else comp_id
                        raise ValueError(
                            f"المخزون غير كافٍ للمادة: {comp_label} — المتاح "
                            f"{comp_state['qty']:.3f} والمطلوب {comp_qty:.3f}"
                        )
                    comp_unit_cost = comp_state["avg"]
                    comp_cost = money.quantized(comp_unit_cost * comp_qty)
                    comp_state["qty"] -= comp_qty
                    if abs(comp_state["qty"]) <= EPSILON:
                        comp_state["qty"] = 0.0
                    conn.execute(
                        """INSERT INTO inventory_movements(
                               item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (comp_id, row["invoice_id"], "sale", -comp_qty, comp_unit_cost, -comp_cost, row["invoice_date"]),
                    )
                    total_cost += comp_cost
                unit_cost = total_cost / float(row["quantity"]) if float(row["quantity"]) > EPSILON else 0.0
                conn.execute(
                    "UPDATE invoice_lines SET unit_cost=?,cost_amount=? WHERE id=?",
                    (unit_cost, money.quantized(total_cost), row["id"]),
                )
            else:
                st = get_state(item_id)
                allow_negative = bool(row.get("allow_negative_stock"))
                if not allow_negative and st["qty"] + EPSILON < base_qty:
                    raise ValueError(
                        f"المخزون غير كافٍ للمادة: {row['item_name']} — المتاح {st['qty']:.3f} والمطلوب {base_qty:.3f}"
                    )
                unit_cost = st["avg"]
                cost_amount = money.quantized(unit_cost * base_qty)
                st["qty"] -= base_qty
                if abs(st["qty"]) <= EPSILON:
                    st["qty"] = 0.0
                conn.execute(
                    "UPDATE invoice_lines SET unit_cost=?,cost_amount=? WHERE id=?",
                    (unit_cost, cost_amount, row["id"]),
                )
                conn.execute(
                    """INSERT INTO inventory_movements(
                           item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, row["invoice_id"], "sale", -base_qty, unit_cost, -cost_amount, row["invoice_date"]),
                )

        for item in conn.execute("SELECT id FROM items WHERE item_type='مخزون'").fetchall():
            st = state.get(int(item["id"]), {"qty": 0.0, "avg": 0.0})
            conn.execute(
                "UPDATE items SET quantity=?,average_cost=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (st["qty"], st["avg"], item["id"]),
            )

    @staticmethod
    def _create_initial_invoice_payment(conn, inv) -> int | None:
        """(Re)create one invoice's derived initial-payment row + allocation."""
        if _is_opening_invoice(inv):
            return None
        initial = float(inv["initial_paid_amount"] or 0)
        total = float(inv["total"] or 0)
        if initial < -EPSILON or initial > total + EPSILON:
            raise ValueError(f"الدفعة الأولى غير صالحة للفاتورة #{inv['id']}")
        if initial <= EPSILON:
            return None
        direction = "in" if inv["type"] == "sale" else "out"
        # Derived reflection of the invoice's own initial payment: inherit the
        # invoice's historical rate snapshot rather than stamping "now".
        cur = conn.execute(
            """INSERT INTO payments(
                   invoice_id,customer_id,supplier_id,direction,amount,payment_date,
                   reference,notes,source_type,source_id,
                   payment_exchange_rate,payment_currency_code,payment_currency_symbol
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                inv["id"],
                inv["customer_id"],
                inv["supplier_id"],
                direction,
                initial,
                inv["invoice_date"],
                inv["reference"],
                "دفعة أولى من الفاتورة",
                "invoice_initial",
                inv["id"],
                inv["invoice_exchange_rate"],
                inv["invoice_currency_code"],
                inv["invoice_currency_symbol"],
            ),
        )
        payment_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO payment_allocations(payment_id,invoice_id,amount) VALUES(?,?,?)",
            (payment_id, inv["id"], initial),
        )
        return payment_id

    @classmethod
    def rebuild_financials(cls, conn, progress=None, cancel=None) -> None:
        cls._sync_initial_invoice_payments(conn, progress=progress, cancel=cancel)
        cls._validate_allocations(conn)
        cls._refresh_invoice_paid_amounts(conn)
        cls._rebuild_ledger_and_party_balances(conn, progress=progress, cancel=cancel)

    @classmethod
    def rebuild_inventory_for_items(cls, conn, item_ids) -> None:
        """Recompute the inventory timeline of just these stock items.

        Used by incremental invoice delete (and, later, edit): replaying one
        item's own movements (plus any composite-sale consumption of it) is
        O(movements of that item) instead of O(all invoice lines).
        """
        ids = sorted({int(i) for i in (item_ids or []) if i is not None})
        if not ids:
            return
        comp_costs: dict[int, float] = {}
        for item_id in ids:
            item = conn.execute(
                "SELECT id,item_type,is_composite,opening_quantity,opening_unit_cost FROM items WHERE id=?",
                (item_id,),
            ).fetchone()
            if item is None or str(item["item_type"]) != "مخزون" or int(item["is_composite"] or 0):
                continue
            conn.execute(
                "DELETE FROM inventory_movements WHERE item_id=? AND invoice_id IS NOT NULL",
                (item_id,),
            )
            qty = float(item["opening_quantity"] or 0)
            avg = float(item["opening_unit_cost"] or 0) if abs(qty) > EPSILON else 0.0
            events: list[tuple] = []
            for r in conn.execute(
                """SELECT il.id AS line_id, il.invoice_id, il.quantity_in_base, il.total,
                          inv.type, inv.invoice_date, inv.created_at, inv.id AS inv_id,
                          inv.allow_negative_stock AS allow_negative_stock
                   FROM invoice_lines il JOIN invoices inv ON inv.id=il.invoice_id
                   WHERE il.item_id=? AND inv.type IN ('purchase','sale')""",
                (item_id,),
            ).fetchall():
                events.append((
                    str(r["invoice_date"]), str(r["created_at"] or ""), int(r["inv_id"]), int(r["line_id"]),
                    r["type"], dict(r),
                ))
            for r in conn.execute(
                """SELECT ilc.invoice_line_id AS line_id, il.invoice_id, ilc.base_qty,
                          inv.invoice_date, inv.created_at, inv.id AS inv_id
                   FROM invoice_line_components ilc
                   JOIN invoice_lines il ON il.id=ilc.invoice_line_id
                   JOIN invoices inv ON inv.id=il.invoice_id
                   WHERE ilc.component_item_id=? AND inv.type='sale'""",
                (item_id,),
            ).fetchall():
                events.append((
                    str(r["invoice_date"]), str(r["created_at"] or ""), int(r["inv_id"]), int(r["line_id"]),
                    "composite", dict(r),
                ))
            events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
            last_purchase_unit_cost: float | None = None
            for _date, _created, inv_id, line_id, kind, data in events:
                if kind == "purchase":
                    base = float(data["quantity_in_base"])
                    line_total = float(data["total"])
                    unit_cost = (line_total / base) if base else 0.0
                    new_qty = qty + base
                    if new_qty > EPSILON:
                        avg = ((qty * avg) + line_total) / new_qty
                    else:
                        avg = unit_cost
                    qty = new_qty
                    last_purchase_unit_cost = unit_cost
                    conn.execute("UPDATE invoice_lines SET unit_cost=?,cost_amount=0 WHERE id=?", (unit_cost, line_id))
                    conn.execute(
                        """INSERT INTO inventory_movements(item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date)
                           VALUES(?,?,'purchase',?,?,?,?)""",
                        (item_id, inv_id, base, unit_cost, line_total, str(data["invoice_date"])),
                    )
                elif kind == "sale":
                    base = float(data["quantity_in_base"])
                    allow_negative = bool(int(data.get("allow_negative_stock") or 0))
                    if not allow_negative and qty + EPSILON < base:
                        name_row = conn.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
                        label = name_row[0] if name_row is not None else item_id
                        raise ValueError(
                            f"المخزون غير كافٍ للمادة: {label} — المتاح {qty:.3f} والمطلوب {base:.3f}"
                        )
                    cost = money.quantized(avg * base)
                    qty -= base
                    if abs(qty) <= EPSILON:
                        qty = 0.0
                    conn.execute("UPDATE invoice_lines SET unit_cost=?,cost_amount=? WHERE id=?", (avg, cost, line_id))
                    conn.execute(
                        """INSERT INTO inventory_movements(item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date)
                           VALUES(?,?,'sale',?,?,?,?)""",
                        (item_id, inv_id, -base, avg, -cost, str(data["invoice_date"])),
                    )
                else:  # composite consumption of this component
                    base = float(data["base_qty"])
                    if qty + EPSILON < base:
                        name_row = conn.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
                        label = name_row[0] if name_row is not None else item_id
                        raise ValueError(
                            f"المخزون غير كافٍ للمادة: {label} — المتاح {qty:.3f} والمطلوب {base:.3f}"
                        )
                    cost = money.quantized(avg * base)
                    qty -= base
                    if abs(qty) <= EPSILON:
                        qty = 0.0
                    conn.execute(
                        """INSERT INTO inventory_movements(item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date)
                           VALUES(?,?,'sale',?,?,?,?)""",
                        (item_id, inv_id, -base, avg, -cost, str(data["invoice_date"])),
                    )
                    comp_costs[line_id] = comp_costs.get(line_id, 0.0) + cost
            if last_purchase_unit_cost is not None:
                conn.execute(
                    "UPDATE items SET quantity=?,average_cost=?,purchase_price=? WHERE id=?",
                    (qty, avg, last_purchase_unit_cost, item_id),
                )
            else:
                conn.execute(
                    "UPDATE items SET quantity=?,average_cost=? WHERE id=?",
                    (qty, avg, item_id),
                )
        # Composite sale lines: their cost is the sum of their components.
        for line_id, cost in comp_costs.items():
            row = conn.execute(
                "SELECT quantity FROM invoice_lines WHERE id=?", (line_id,)
            ).fetchone()
            qty = float(row["quantity"] or 0) if row is not None else 0.0
            unit_cost = (cost / qty) if qty > EPSILON else 0.0
            conn.execute(
                "UPDATE invoice_lines SET unit_cost=?,cost_amount=? WHERE id=?",
                (unit_cost, money.quantized(cost), line_id),
            )

    @classmethod
    def remove_invoice(cls, conn, invoice_id: int, row, item_ids) -> bool:
        """Incrementally unpost a deleted invoice (fallback: return False).

        Reverses its ledger, its derived initial payment and the party balance,
        removes its inventory movements, deletes the invoice, then replays only
        the affected items' timelines (including composite consumption).
        Returns ``False`` for opening invoices so the caller falls back to the
        full rebuild. Composite *lines* are rejected by the caller.
        """
        invoice_id = int(invoice_id)
        if row is None or _is_opening_invoice(row):
            return False
        conn.execute(
            "DELETE FROM ledger_entries WHERE source_type='invoice' AND source_id=?", (invoice_id,)
        )
        for p in conn.execute(
            "SELECT * FROM payments WHERE source_type='invoice_initial' AND invoice_id=?", (invoice_id,)
        ).fetchall():
            conn.execute("DELETE FROM ledger_entries WHERE source_type='payment' AND source_id=?", (int(p["id"]),))
            amount = money.quantized(float(p["amount"]))
            if str(p["direction"]) == "in" and p["customer_id"]:
                conn.execute("UPDATE customers SET balance=balance+? WHERE id=?", (amount, p["customer_id"]))
            elif str(p["direction"]) == "out" and p["supplier_id"]:
                conn.execute("UPDATE suppliers SET balance=balance+? WHERE id=?", (amount, p["supplier_id"]))
        conn.execute("DELETE FROM payments WHERE source_type='invoice_initial' AND invoice_id=?", (invoice_id,))
        total = money.quantized(float(row["total"] or 0))
        if str(row["type"]) == "sale" and row["customer_id"]:
            conn.execute("UPDATE customers SET balance=balance-? WHERE id=?", (total, row["customer_id"]))
        elif str(row["type"]) == "purchase" and row["supplier_id"]:
            conn.execute("UPDATE suppliers SET balance=balance-? WHERE id=?", (total, row["supplier_id"]))
        conn.execute("DELETE FROM inventory_movements WHERE invoice_id=?", (invoice_id,))
        conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        cls.rebuild_inventory_for_items(conn, item_ids)
        derived_state.mark_clean_conn(conn)
        return True

    @staticmethod
    def refresh_paid_amounts(conn, invoice_ids) -> None:
        for invoice_id in {int(i) for i in (invoice_ids or []) if i is not None}:
            conn.execute(
                "UPDATE invoices SET paid_amount=COALESCE("
                "(SELECT SUM(amount) FROM payment_allocations WHERE invoice_id=?),0) WHERE id=?",
                (invoice_id, invoice_id),
            )

    @classmethod
    def post_payment(cls, conn, payment_id: int) -> None:
        """Incrementally post one cash movement (voucher create/update)."""
        payment_id = int(payment_id)
        pay = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if pay is None:
            return
        cls._write_payment_ledger(
            conn, payment_id, str(pay["direction"]), float(pay["amount"]),
            str(pay["payment_date"]), pay["customer_id"], pay["supplier_id"],
            str(pay["notes"] or ""),
        )
        amount = money.quantized(float(pay["amount"]))
        if str(pay["direction"]) == "in" and pay["customer_id"]:
            conn.execute("UPDATE customers SET balance=balance-? WHERE id=?", (amount, pay["customer_id"]))
        elif str(pay["direction"]) == "out" and pay["supplier_id"]:
            conn.execute("UPDATE suppliers SET balance=balance-? WHERE id=?", (amount, pay["supplier_id"]))
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT invoice_id FROM payment_allocations WHERE payment_id=?", (payment_id,)
        ).fetchall()]
        cls.refresh_paid_amounts(conn, ids)
        derived_state.mark_clean_conn(conn)

    @classmethod
    def remove_payment(cls, conn, payment_row, invoice_ids=None) -> None:
        """Incrementally unpost one cash movement (voucher delete/update)."""
        if payment_row is None:
            return
        payment_id = int(payment_row["id"])
        conn.execute(
            "DELETE FROM ledger_entries WHERE source_type='payment' AND source_id=?", (payment_id,)
        )
        amount = money.quantized(float(payment_row["amount"] or 0))
        if str(payment_row["direction"]) == "in" and payment_row["customer_id"]:
            conn.execute("UPDATE customers SET balance=balance+? WHERE id=?", (amount, payment_row["customer_id"]))
        elif str(payment_row["direction"]) == "out" and payment_row["supplier_id"]:
            conn.execute("UPDATE suppliers SET balance=balance+? WHERE id=?", (amount, payment_row["supplier_id"]))
        if invoice_ids is None:
            invoice_ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT invoice_id FROM payment_allocations WHERE payment_id=?", (payment_id,)
            ).fetchall()]
        cls.refresh_paid_amounts(conn, invoice_ids)
        derived_state.mark_clean_conn(conn)

    @classmethod
    def append_invoice(cls, conn, invoice_id: int) -> bool:
        """Fast incremental post for a *newly created* invoice.

        Returns ``True`` when the invoice could be posted incrementally (the
        common POS case: dated today, no composite lines, no back-dating).
        Returns ``False`` when the caller must fall back to the full rebuild
        (composites, back-dated invoices, edited/deleted documents). This keeps
        writes on a 300k-invoice database in the millisecond range instead of
        re-deriving the whole database.
        """
        inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if inv is None or str(inv["type"]) not in {"sale", "purchase"}:
            return False
        if _is_opening_invoice(inv):
            return False
        date = str(inv["invoice_date"] or "")
        lines = conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", (invoice_id,)
        ).fetchall()
        state: dict[int, dict[str, float]] = {}
        stock_lines: list[tuple] = []
        for ln in lines:
            item_id = ln["item_id"]
            if not item_id:
                continue
            item = conn.execute(
                "SELECT id,item_type,is_composite,quantity,average_cost FROM items WHERE id=?",
                (item_id,),
            ).fetchone()
            if item is None or str(item["item_type"]) != "مخزون":
                continue
            if int(item["is_composite"] or 0):
                # Composite sales consume their components' timeline; let the
                # full rebuild handle them so stock/COGS always stays exact.
                return False
            if int(item_id) not in state:
                state[int(item_id)] = {
                    "qty": float(item["quantity"] or 0),
                    "avg": float(item["average_cost"] or 0),
                }
            stock_lines.append((ln, int(item_id)))
        # Only append when this invoice is chronologically last for every item
        # it touches; a back-dated invoice would change history and needs replay.
        for _ln, item_id in stock_lines:
            latest = conn.execute(
                """SELECT MAX(inv.invoice_date) FROM invoice_lines il
                   JOIN invoices inv ON inv.id=il.invoice_id
                   WHERE il.item_id=? AND inv.id<>?""",
                (item_id, invoice_id),
            ).fetchone()[0]
            latest_comp = conn.execute(
                """SELECT MAX(inv.invoice_date) FROM invoice_line_components ilc
                   JOIN invoice_lines il ON il.id=ilc.invoice_line_id
                   JOIN invoices inv ON inv.id=il.invoice_id
                   WHERE ilc.component_item_id=? AND inv.id<>?""",
                (item_id, invoice_id),
            ).fetchone()[0]
            for other in (latest, latest_comp):
                if other and str(other) > date:
                    return False
        # ---- inventory ----
        allow_negative = bool(int(inv["allow_negative_stock"] or 0)) if "allow_negative_stock" in inv.keys() else False
        for ln, item_id in stock_lines:
            base_qty = float(ln["quantity_in_base"])
            line_total = float(ln["total"])
            st = state[item_id]
            if str(inv["type"]) == "purchase":
                unit_cost = (line_total / base_qty) if base_qty else 0.0
                new_qty = st["qty"] + base_qty
                if new_qty > EPSILON:
                    st["avg"] = ((st["qty"] * st["avg"]) + line_total) / new_qty
                else:
                    st["avg"] = unit_cost
                st["qty"] = new_qty
                conn.execute(
                    "UPDATE invoice_lines SET unit_cost=?,cost_amount=0 WHERE id=?",
                    (unit_cost, ln["id"]),
                )
                conn.execute(
                    """INSERT INTO inventory_movements(
                           item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date
                       ) VALUES(?,?,'purchase',?,?,?,?)""",
                    (item_id, invoice_id, base_qty, unit_cost, line_total, date),
                )
                conn.execute(
                    "UPDATE items SET quantity=?,average_cost=?,purchase_price=? WHERE id=?",
                    (st["qty"], st["avg"], unit_cost, item_id),
                )
            else:
                if not allow_negative and st["qty"] + EPSILON < base_qty:
                    name_row = conn.execute("SELECT name FROM items WHERE id=?", (item_id,)).fetchone()
                    label = name_row[0] if name_row is not None else item_id
                    raise ValueError(
                        f"المخزون غير كافٍ للمادة: {label} — المتاح {st['qty']:.3f} والمطلوب {base_qty:.3f}"
                    )
                unit_cost = st["avg"]
                cost = money.quantized(unit_cost * base_qty)
                st["qty"] = st["qty"] - base_qty
                if abs(st["qty"]) <= EPSILON:
                    st["qty"] = 0.0
                conn.execute(
                    "UPDATE invoice_lines SET unit_cost=?,cost_amount=? WHERE id=?",
                    (unit_cost, cost, ln["id"]),
                )
                conn.execute(
                    """INSERT INTO inventory_movements(
                           item_id,invoice_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date
                       ) VALUES(?,?,'sale',?,?,?,?)""",
                    (item_id, invoice_id, -base_qty, unit_cost, -cost, date),
                )
                conn.execute(
                    "UPDATE items SET quantity=?,average_cost=? WHERE id=?",
                    (st["qty"], st["avg"], item_id),
                )
        # ---- invoice ledger + party balance ----
        total = float(inv["total"])
        if str(inv["type"]) == "sale":
            conn.execute(
                """INSERT INTO ledger_entries(entry_date,account_code,party_type,party_id,debit,source_type,source_id,description)
                   VALUES(?,?,?,?,?,'invoice',?,?)""",
                (date, "AR", "customer" if inv["customer_id"] else None,
                 inv["customer_id"], total, invoice_id, "فاتورة بيع"),
            )
            conn.execute(
                "INSERT INTO ledger_entries(entry_date,account_code,credit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                (date, "SALES", total, invoice_id, "إيراد مبيعات"),
            )
            cogs = float(conn.execute(
                "SELECT COALESCE(SUM(cost_amount),0) FROM invoice_lines WHERE invoice_id=?",
                (invoice_id,),
            ).fetchone()[0])
            if cogs > EPSILON:
                conn.execute(
                    "INSERT INTO ledger_entries(entry_date,account_code,debit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                    (date, "COGS", cogs, invoice_id, "تكلفة بضاعة مباعة"),
                )
                conn.execute(
                    "INSERT INTO ledger_entries(entry_date,account_code,credit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                    (date, "INVENTORY", cogs, invoice_id, "انخفاض مخزون"),
                )
            if inv["customer_id"]:
                conn.execute(
                    "UPDATE customers SET balance=balance+? WHERE id=?",
                    (money.quantized(total), inv["customer_id"]),
                )
        else:
            conn.execute(
                "INSERT INTO ledger_entries(entry_date,account_code,debit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                (date, "PURCHASES", total, invoice_id, "فاتورة شراء"),
            )
            conn.execute(
                """INSERT INTO ledger_entries(entry_date,account_code,party_type,party_id,credit,source_type,source_id,description)
                   VALUES(?,?,?,?,?,'invoice',?,?)""",
                (date, "AP", "supplier" if inv["supplier_id"] else None,
                 inv["supplier_id"], total, invoice_id, "التزام مورد"),
            )
            if inv["supplier_id"]:
                conn.execute(
                    "UPDATE suppliers SET balance=balance+? WHERE id=?",
                    (money.quantized(total), inv["supplier_id"]),
                )
        # ---- derived initial payment + its cash ledger ----
        payment_id = cls._create_initial_invoice_payment(conn, inv)
        if payment_id is not None:
            pay = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
            if pay is not None:
                cls._write_payment_ledger(
                    conn, payment_id, str(pay["direction"]), float(pay["amount"]),
                    str(pay["payment_date"]), pay["customer_id"], pay["supplier_id"],
                    str(pay["notes"] or ""),
                )
                amount = money.quantized(float(pay["amount"]))
                if pay["direction"] == "in" and pay["customer_id"]:
                    conn.execute("UPDATE customers SET balance=balance-? WHERE id=?", (amount, pay["customer_id"]))
                elif pay["direction"] == "out" and pay["supplier_id"]:
                    conn.execute("UPDATE suppliers SET balance=balance-? WHERE id=?", (amount, pay["supplier_id"]))
        conn.execute(
            "UPDATE invoices SET paid_amount=COALESCE((SELECT SUM(amount) FROM payment_allocations WHERE invoice_id=?),0) WHERE id=?",
            (invoice_id, invoice_id),
        )
        derived_state.mark_clean_conn(conn)
        return True

    @staticmethod
    def _sync_initial_invoice_payments(conn, progress=None, cancel=None) -> None:
        # Initial payments are source-document fields on invoices. Regenerate
        # their cash rows and allocations deterministically on every rebuild.
        conn.execute("DELETE FROM payments WHERE source_type='invoice_initial'")

        invoices = conn.execute(
            "SELECT * FROM invoices ORDER BY invoice_date, created_at, id"
        ).fetchall()
        total_invoices = len(invoices)
        if progress:
            progress("rebuild_initial_payments", 0, float(total_invoices or 1), "invoices", "مزامنة الدفعات الأولية")
        for inv_index, inv in enumerate(invoices, start=1):
            check_cancel(cancel)
            if progress and inv_index % 2000 == 0:
                progress("rebuild_initial_payments", inv_index, float(total_invoices), "invoices", "مزامنة الدفعات الأولية")
            if _is_opening_invoice(inv):
                # Opening stock is not a purchased-in-period document: skip
                # its derived initial payment entirely.
                continue
            # Deliberately NOT money.quantized here: stored initial/total were
            # already quantized to cents at write time for every modern row.
            # Legacy rate-derived rows (51000 SYP / 13500 = 3.7760000000000002)
            # hold raw recurring totals; quantizing only the initial payment
            # against them made valid fully-paid invoices fail every rebuild
            # with "الدفعة الأولى غير صالحة" (raise) — or worse, silently
            # rewrite the derived payment to the cent value and leave a
            # phantom "partial" remaining balance. Raw-vs-raw keeps derived
            # rows (payment, allocation, paid_amount) exactly equal to the
            # stored total whenever the stored initial was.
            initial = float(inv["initial_paid_amount"] or 0)
            total = float(inv["total"] or 0)
            if initial < -EPSILON or initial > total + EPSILON:
                raise ValueError(f"الدفعة الأولى غير صالحة للفاتورة #{inv['id']}")
            AccountingRebuilder._create_initial_invoice_payment(conn, inv)

    @staticmethod
    def _validate_allocations(conn) -> None:
        payment_sums = {
            int(r["payment_id"]): float(r["allocated"] or 0)
            for r in conn.execute(
                "SELECT payment_id,SUM(amount) AS allocated FROM payment_allocations GROUP BY payment_id"
            ).fetchall()
        }
        for p in conn.execute("SELECT * FROM payments").fetchall():
            allocated = payment_sums.get(int(p["id"]), 0.0)
            # Same quantized-vs-quantized discipline as _sync_initial_invoice_payments,
            # compared at CENT granularity on purpose: legacy payment/total rows
            # can carry raw rate-derived floats (3.7760000000000002) next to
            # cent-quantized allocations (3.78). At the 6-place storage quantum
            # those are genuinely different amounts, but flagging them would
            # break rebuild/restore for every pre-existing database -- and the
            # guard's job is catching real over-allocation (>= 1 cent), not
            # representation dust from the old quantum.
            if money.quantized(allocated, decimals=2) > money.quantized(p["amount"], decimals=2) + EPSILON:
                raise ValueError(f"توزيع الدفعة #{p['id']} يتجاوز قيمة الدفعة")

        invoice_sums = {
            int(r["invoice_id"]): float(r["allocated"] or 0)
            for r in conn.execute(
                "SELECT invoice_id,SUM(amount) AS allocated FROM payment_allocations GROUP BY invoice_id"
            ).fetchall()
        }
        for inv in conn.execute("SELECT * FROM invoices").fetchall():
            allocated = invoice_sums.get(int(inv["id"]), 0.0)
            # Cent granularity, same reasoning as the payment-level guard
            # above: forgive legacy representation dust, flag real
            # over-allocation.
            if money.quantized(allocated, decimals=2) > money.quantized(inv["total"], decimals=2) + EPSILON:
                raise ValueError(
                    f"الدفعات الموزعة على الفاتورة #{inv['id']} تتجاوز إجمالي الفاتورة؛ عدّل السندات المرتبطة أولًا"
                )

        rows = conn.execute(
            """
            SELECT pa.id, p.direction,p.customer_id AS p_customer,p.supplier_id AS p_supplier,
                   i.type,i.customer_id AS i_customer,i.supplier_id AS i_supplier
            FROM payment_allocations pa
            JOIN payments p ON p.id=pa.payment_id
            JOIN invoices i ON i.id=pa.invoice_id
            """
        ).fetchall()
        for row in rows:
            if row["direction"] == "in":
                if row["type"] != "sale":
                    raise ValueError("لا يمكن توزيع سند قبض على فاتورة شراء")
                if row["p_customer"] is not None and row["i_customer"] != row["p_customer"]:
                    raise ValueError("سند القبض مرتبط بعميل مختلف عن الفاتورة")
            else:
                if row["type"] != "purchase":
                    raise ValueError("لا يمكن توزيع سند صرف على فاتورة بيع")
                if row["p_supplier"] is not None and row["i_supplier"] != row["p_supplier"]:
                    raise ValueError("سند الصرف مرتبط بمورد مختلف عن الفاتورة")

    @staticmethod
    def _refresh_invoice_paid_amounts(conn) -> None:
        conn.execute(
            """UPDATE invoices
               SET paid_amount=COALESCE((
                   SELECT SUM(pa.amount) FROM payment_allocations pa WHERE pa.invoice_id=invoices.id
               ),0)
               WHERE notes IS NULL OR notes NOT LIKE '%__opening_balance__%'"""
        )

    @classmethod
    def _rebuild_ledger_and_party_balances(cls, conn, progress=None, cancel=None) -> None:
        conn.execute("DELETE FROM ledger_entries WHERE source_type IN ('invoice','payment','opening')")
        conn.execute("UPDATE customers SET balance=0,updated_at=CURRENT_TIMESTAMP")
        conn.execute("UPDATE suppliers SET balance=0,updated_at=CURRENT_TIMESTAMP")

        invoices = conn.execute(
            "SELECT * FROM invoices ORDER BY invoice_date, created_at, id"
        ).fetchall()
        total_invoices = len(invoices)
        if progress:
            progress("rebuild_ledger_invoices", 0, float(total_invoices or 1), "invoices", "ترحيل قيود الفواتير")
        for inv_index, inv in enumerate(invoices, start=1):
            check_cancel(cancel)
            if progress and inv_index % 2000 == 0:
                progress("rebuild_ledger_invoices", inv_index, float(total_invoices), "invoices", "ترحيل قيود الفواتير")
            if _is_opening_invoice(inv):
                # Inventory-only: the opening invoice already seeded stock in
                # rebuild_inventory; posting PURCHASES/AP or moving cash here
                # would invent a period expense/liability that never happened.
                continue
            invoice_id = int(inv["id"])
            total = float(inv["total"])
            if inv["type"] == "sale":
                conn.execute(
                    """INSERT INTO ledger_entries(
                           entry_date,account_code,party_type,party_id,debit,source_type,source_id,description
                       ) VALUES(?,?,?,?,?,'invoice',?,?)""",
                    (
                        inv["invoice_date"], "AR",
                        "customer" if inv["customer_id"] else None,
                        inv["customer_id"], total, invoice_id, "فاتورة بيع",
                    ),
                )
                conn.execute(
                    "INSERT INTO ledger_entries(entry_date,account_code,credit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                    (inv["invoice_date"], "SALES", total, invoice_id, "إيراد مبيعات"),
                )
                cogs = float(conn.execute(
                    "SELECT COALESCE(SUM(cost_amount),0) FROM invoice_lines WHERE invoice_id=?",
                    (invoice_id,),
                ).fetchone()[0])
                if cogs > EPSILON:
                    conn.execute(
                        "INSERT INTO ledger_entries(entry_date,account_code,debit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                        (inv["invoice_date"], "COGS", cogs, invoice_id, "تكلفة بضاعة مباعة"),
                    )
                    conn.execute(
                        "INSERT INTO ledger_entries(entry_date,account_code,credit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                        (inv["invoice_date"], "INVENTORY", cogs, invoice_id, "انخفاض مخزون"),
                    )
                if inv["customer_id"]:
                    conn.execute("UPDATE customers SET balance=balance+? WHERE id=?", (money.quantized(total), inv["customer_id"]))
            else:
                conn.execute(
                    "INSERT INTO ledger_entries(entry_date,account_code,debit,source_type,source_id,description) VALUES(?,?,?,'invoice',?,?)",
                    (inv["invoice_date"], "PURCHASES", total, invoice_id, "فاتورة شراء"),
                )
                conn.execute(
                    """INSERT INTO ledger_entries(
                           entry_date,account_code,party_type,party_id,credit,source_type,source_id,description
                       ) VALUES(?,?,?,?,?,'invoice',?,?)""",
                    (
                        inv["invoice_date"], "AP",
                        "supplier" if inv["supplier_id"] else None,
                        inv["supplier_id"], total, invoice_id, "التزام مورد",
                    ),
                )
                if inv["supplier_id"]:
                    conn.execute("UPDATE suppliers SET balance=balance+? WHERE id=?", (money.quantized(total), inv["supplier_id"]))

        payments = conn.execute("SELECT * FROM payments ORDER BY payment_date, created_at, id").fetchall()
        total_payments = len(payments)
        if progress:
            progress("rebuild_ledger_payments", 0, float(total_payments or 1), "payments", "ترحيل قيود الدفعات والسندات")
        for pay_index, p in enumerate(payments, start=1):
            check_cancel(cancel)
            if progress and pay_index % 2000 == 0:
                progress("rebuild_ledger_payments", pay_index, float(total_payments), "payments", "ترحيل قيود الدفعات والسندات")
            amount = money.quantized(p["amount"])
            cls._write_payment_ledger(
                conn, int(p["id"]), str(p["direction"]), amount,
                str(p["payment_date"]), p["customer_id"], p["supplier_id"],
                str(p["notes"] or ""),
            )
            if p["direction"] == "in" and p["customer_id"]:
                conn.execute("UPDATE customers SET balance=balance-? WHERE id=?", (amount, p["customer_id"]))
            elif p["direction"] == "out" and p["supplier_id"]:
                conn.execute("UPDATE suppliers SET balance=balance-? WHERE id=?", (amount, p["supplier_id"]))

        # Party opening balances: posted last so each party's balance can be
        # re-derived from the *full* ledger (invoices/payments + opening) in
        # one place. They never touch cash or the income statement.
        for row in conn.execute("SELECT id FROM customers").fetchall():
            cls.sync_party_opening(conn, "customers", party_id=int(row["id"]))
        for row in conn.execute("SELECT id FROM suppliers").fetchall():
            cls.sync_party_opening(conn, "suppliers", party_id=int(row["id"]))

    @classmethod
    def sync_party_opening(cls, conn, table: str, *, party_id: int) -> None:
        """(Re)post one party's opening-balance ledger pair from its columns.

        The signed ``opening_balance`` is the party's *natural* balance: a
        positive customer value is a receivable (debit AR), a positive supplier
        value is a payable (credit AP). The offset is ``OPENING_EQUITY`` so the
        entry is always balanced and never posts cash/P&L. The party's
        ``balance`` is then re-derived from the whole ledger, keeping this
        correct whether it runs after a party save or inside a full rebuild.
        """
        party_type = "customer" if table == "customers" else "supplier"
        account = "AR" if party_type == "customer" else "AP"
        conn.execute(
            "DELETE FROM ledger_entries WHERE source_type='opening' AND party_type=? AND party_id=?",
            (party_type, party_id),
        )
        row = conn.execute(
            f"SELECT name,opening_balance,opening_balance_date FROM {table} WHERE id=?",
            (party_id,),
        ).fetchone()
        if row is None:
            return
        balance = float(row["opening_balance"] or 0)
        entry_date = str(row["opening_balance_date"] or "").strip() or date.today().isoformat()
        if abs(balance) > EPSILON:
            amount = abs(balance)
            if party_type == "customer":
                party_debit = amount if balance > 0 else 0.0
                party_credit = amount if balance < 0 else 0.0
            else:
                party_debit = amount if balance < 0 else 0.0
                party_credit = amount if balance > 0 else 0.0
            label = f"رصيد افتتاحي — {row['name']}"
            conn.execute(
                """INSERT INTO ledger_entries(
                       entry_date,account_code,party_type,party_id,debit,credit,source_type,source_id,description
                   ) VALUES(?,?,?,?,?,?, 'opening', ?, ?)""",
                (entry_date, account, party_type, party_id, party_debit, party_credit, party_id, label),
            )
            conn.execute(
                """INSERT INTO ledger_entries(
                       entry_date,account_code,party_type,party_id,debit,credit,source_type,source_id,description
                   ) VALUES(?,?,?,?,?,?, 'opening', ?, ?)""",
                (entry_date, OPENING_EQUITY_ACCOUNT, party_type, party_id, party_credit, party_debit, party_id, label),
            )
        raw = float(conn.execute(
            "SELECT COALESCE(SUM(debit-credit),0) FROM ledger_entries"
            " WHERE party_type=? AND party_id=? AND account_code=?",
            (party_type, party_id, account),
        ).fetchone()[0])
        natural = raw if party_type == "customer" else -raw
        conn.execute(f"UPDATE {table} SET balance=? WHERE id=?", (money.quantized(natural), party_id))

    @staticmethod
    def _write_payment_ledger(
        conn,
        payment_id: int,
        direction: str,
        amount: float,
        payment_date: str,
        customer_id: int | None,
        supplier_id: int | None,
        notes: str = "",
    ) -> None:
        if direction == "in":
            conn.execute(
                "INSERT INTO ledger_entries(entry_date,account_code,debit,source_type,source_id,description) VALUES(?,?,?,'payment',?,?)",
                (payment_date, "CASH", amount, payment_id, notes or "قبض نقدي"),
            )
            conn.execute(
                """INSERT INTO ledger_entries(
                       entry_date,account_code,party_type,party_id,credit,source_type,source_id,description
                   ) VALUES(?,?,?,?,?,'payment',?,?)""",
                (
                    payment_date, "AR", "customer" if customer_id else None,
                    customer_id, amount, payment_id, notes or "تسديد عميل",
                ),
            )
        else:
            conn.execute(
                """INSERT INTO ledger_entries(
                       entry_date,account_code,party_type,party_id,debit,source_type,source_id,description
                   ) VALUES(?,?,?,?,?,'payment',?,?)""",
                (
                    payment_date, "AP", "supplier" if supplier_id else None,
                    supplier_id, amount, payment_id, notes or "تسديد مورد",
                ),
            )
            conn.execute(
                "INSERT INTO ledger_entries(entry_date,account_code,credit,source_type,source_id,description) VALUES(?,?,?,'payment',?,?)",
                (payment_date, "CASH", amount, payment_id, notes or "صرف نقدي"),
            )
