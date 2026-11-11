from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from nano_offline.core.database import Database
from nano_offline.core import currency
from nano_offline.core import event_bus
from nano_offline.core import invoice_settings
from nano_offline.core import money
from nano_offline.core import restaurant_settings
from nano_offline.core.expiring import invalidate_insights
from nano_offline.services.accounting_rebuilder import AccountingRebuilder, OPENING_BALANCE_MARKER

EPSILON = 1e-9
_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class InvoiceLineInput:
    description: str
    quantity: float
    unit_price: float
    item_id: int | None = None
    unit_id: int | None = None
    conversion_factor: float = 1.0


class InvoiceService:
    """Offline invoice/accounting core.

    Invoices are the source of truth. After create/update/delete, inventory,
    invoice-linked payments, party balances and ledger entries are rebuilt
    inside the same SQLite transaction. This makes editing historical invoices
    deterministic and prevents partial accounting/inventory reversals.
    """

    def __init__(self, db: Database):
        self.db = db
        # Optional station-printing hook set by AppContext. Kept as a plain
        # attribute so the accounting core stays usable without printing.
        self.section_print = None

    def check_purchase_price(
        self,
        *,
        item_id: int | None,
        unit_price: float,
        conversion_factor: float = 1.0,
        supplier_id: int | None = None,
        exclude_invoice_id: int | None = None,
    ) -> dict:
        """Compare a purchase line's per-base-unit price against recent history.

        Thin service wrapper around ``core.price_fingerprint`` so the invoice
        editor never has to reach into ``ctx.db`` directly.
        """
        from nano_offline.core.price_fingerprint import check_purchase_price

        return check_purchase_price(
            self.db,
            item_id=item_id,
            unit_price=unit_price,
            conversion_factor=conversion_factor,
            supplier_id=supplier_id,
            exclude_invoice_id=exclude_invoice_id,
        )

    def list_invoices(self, invoice_type: str | None = None, limit: int = 200) -> list[dict]:
        sql = """
        SELECT i.*,
               c.name AS customer_name,
               s.name AS supplier_name,
               CASE WHEN i.type='sale' THEN c.name ELSE s.name END AS party_name,
               (i.total-i.paid_amount) AS remaining_amount
        FROM invoices i
        LEFT JOIN customers c ON c.id=i.customer_id
        LEFT JOIN suppliers s ON s.id=i.supplier_id
        """
        params: list[object] = []
        if invoice_type in {"sale", "purchase"}:
            sql += " WHERE i.type=?"
            params.append(invoice_type)
        sql += " ORDER BY i.invoice_date DESC, i.id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for row in rows:
            row["payment_status"] = self._payment_status(float(row["total"]), float(row["paid_amount"]))
        return rows

    def get_invoice(self, invoice_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT i.*, c.name AS customer_name, s.name AS supplier_name,
                       CASE WHEN i.type='sale' THEN c.name ELSE s.name END AS party_name,
                       (i.total-i.paid_amount) AS remaining_amount
                FROM invoices i
                LEFT JOIN customers c ON c.id=i.customer_id
                LEFT JOIN suppliers s ON s.id=i.supplier_id
                WHERE i.id=?
                """,
                (invoice_id,),
            ).fetchone()
            if row is None:
                return None
            invoice = dict(row)
            invoice["payment_status"] = self._payment_status(float(invoice["total"]), float(invoice["paid_amount"]))
            invoice["lines"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT il.*, i.name AS item_name, u.name AS unit_name,
                           u.abbreviation AS unit_abbreviation
                    FROM invoice_lines il
                    LEFT JOIN items i ON i.id=il.item_id
                    LEFT JOIN units u ON u.id=il.unit_id
                    WHERE il.invoice_id=?
                    ORDER BY il.id
                    """,
                    (invoice_id,),
                ).fetchall()
            ]
            return invoice

    def create_invoice(
        self,
        *,
        invoice_type: str,
        lines: list[InvoiceLineInput],
        customer_id: int | None = None,
        supplier_id: int | None = None,
        invoice_date: str | None = None,
        reference: str | None = None,
        notes: str | None = None,
        paid_amount: float = 0,
        allow_negative_stock: bool | None = False,
    ) -> int:
        inv_date = invoice_date or date.today().isoformat()
        with self.db.transaction() as conn:
            prepared, total, paid_amount = self._validate_and_prepare(
                conn,
                invoice_type=invoice_type,
                lines=lines,
                customer_id=customer_id,
                supplier_id=supplier_id,
                paid_amount=paid_amount,
            )
            settings = {
                str(r["key"]): str(r["value"])
                for r in conn.execute("SELECT key,value FROM settings").fetchall()
            }
            # Restaurant mode: an unpaid sale with no customer becomes an open
            # "preparation order" attributed to a shared party, so the kitchen
            # tickets print now and the amount is settled later.
            if (
                invoice_type == "sale"
                and customer_id is None
                and paid_amount < total - EPSILON
                and restaurant_settings.enabled(settings)
            ):
                customer_id = self._ensure_restaurant_party(conn)
            paid_amount = self._cash_paid_without_party(
                invoice_type, total, paid_amount, customer_id, supplier_id
            )
            self._require_party_for_credit(invoice_type, total, paid_amount, customer_id, supplier_id)
            print_snapshot = invoice_settings.encode_print_snapshot(
                invoice_settings.print_snapshot_from_settings(settings)
            )
            cur = conn.execute(
                """INSERT INTO invoices(type,customer_id,supplier_id,invoice_date,reference,notes,total,
                                         initial_paid_amount,paid_amount,allow_negative_stock,
                                         invoice_exchange_rate,invoice_currency_code,invoice_currency_symbol,
                                         print_options)
                   VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                (
                    invoice_type,
                    customer_id,
                    supplier_id,
                    inv_date,
                    (reference or "").strip() or None,
                    (notes or "").strip() or None,
                    total,
                    paid_amount,
                    1 if (allow_negative_stock and invoice_type == "sale") else 0,
                    # Record the *real* SYP-per-USD rate regardless of the
                    # display currency at issuance, so a USD invoice still
                    # stores (and prints) the configured rate instead of being
                    # pinned to 1.0 when the admin later changes the rate.
                    currency.get_exchange_rate(settings),
                    currency.get_display_currency(settings),
                    currency.get_display_symbol(settings),
                    print_snapshot,
                ),
            )
            invoice_id = int(cur.lastrowid)
            self._insert_lines(conn, invoice_id, prepared)
            AccountingRebuilder.rebuild(conn)
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('create','invoice',?,?)",
                (invoice_id, f"{invoice_type}:{total}"),
            )
            # Dashboard insights (receivables, pulse, alerts) are cached for
            # a short TTL; a fresh sale must not wait it out.
            invalidate_insights()
        event_bus.publish(event_bus.INVOICE_CREATED, {"invoice_id": invoice_id, "invoice_type": invoice_type, "total": total})
        if invoice_type == "sale" and self.section_print is not None:
            try:
                self.section_print.enqueue_invoice(invoice_id)
            except Exception:
                # Printing is best-effort: a station/printer problem must
                # never turn a successfully saved sale into an error.
                _LOG.exception("station print enqueue failed for invoice %s", invoice_id)
        return invoice_id

    def convert_opening_balances(
        self, *, item_ids: list[int] | None = None, reference: str = "أول المدة"
    ) -> int | None:
        """Create or refresh inventory-only purchase invoices for opening stock.

        Items created with an opening quantity carry it in
        ``items.opening_quantity`` (the baseline ``AccountingRebuilder``
        replays). This converts eligible items into a cash-free "أول المدة"
        purchase invoice. If an item's balance was edited after conversion,
        rerunning this action folds that adjustment into its existing opening
        invoice instead of creating a duplicate or leaving the invoice stale.

        The invoice is marked (:data:`OPENING_BALANCE_MARKER`) so the rebuilder
        treats it as inventory-only: it seeds stock/valuation and shows up in
        the purchases list, but posts no cash, payables or period profit.
        Returns the new/updated invoice id, or ``None`` when nothing changed.
        """
        with self.db.transaction() as conn:
            existing_params: list[object] = [EPSILON, OPENING_BALANCE_MARKER]
            existing_filter = ""
            if item_ids:
                filtered_ids = [int(i) for i in item_ids]
                existing_filter = " AND i.id IN (" + ",".join("?" for _ in filtered_ids) + ")"
                existing_params.extend(filtered_ids)
            existing_lines = conn.execute(
                """SELECT i.id AS item_id,i.name,i.opening_quantity,i.opening_unit_cost,
                          il.id AS line_id,il.invoice_id,il.quantity_in_base,
                          il.conversion_factor,il.total
                   FROM items i
                   JOIN invoice_lines il ON il.item_id=i.id
                   JOIN invoices inv ON inv.id=il.invoice_id
                   WHERE i.item_type='مخزون' AND i.is_composite=0 AND ABS(i.opening_quantity)>?
                     AND instr(COALESCE(inv.notes,''),?)>0""" + existing_filter +
                " ORDER BY il.invoice_id,il.id",
                existing_params,
            ).fetchall()

            grouped_lines: dict[int, list[dict]] = {}
            for row in existing_lines:
                grouped_lines.setdefault(int(row["item_id"]), []).append(dict(row))

            affected_invoice_ids: set[int] = set()
            reconciled_item_ids: set[int] = set()
            updated_invoice_id: int | None = None
            for current_item_id, rows in grouped_lines.items():
                if len(rows) != 1:
                    raise ValueError(
                        f"لا يمكن تحديث «أول المدة» للمادة «{rows[0]['name']}» "
                        "لوجود أكثر من بند افتتاحي لها"
                    )

                row = rows[0]
                old_base_qty = float(row["quantity_in_base"] or 0)
                adjustment_qty = float(row["opening_quantity"] or 0)
                new_base_qty = old_base_qty + adjustment_qty
                factor = float(row["conversion_factor"] or 1)
                new_value = float(row["total"] or 0) + (
                    adjustment_qty * float(row["opening_unit_cost"] or 0)
                )
                if new_base_qty < -EPSILON or new_value < -EPSILON:
                    raise ValueError(
                        f"لا يمكن تحديث فاتورة «أول المدة» للمادة «{row['name']}» "
                        "إلى رصيد أو قيمة سالبة"
                    )

                if new_base_qty <= EPSILON:
                    conn.execute("DELETE FROM invoice_lines WHERE id=?", (row["line_id"],))
                else:
                    display_qty = new_base_qty / factor
                    unit_price = money.quantized((new_value * factor) / new_base_qty)
                    line_total = money.quantized(display_qty * unit_price)
                    conn.execute(
                        """UPDATE invoice_lines
                           SET quantity=?,quantity_in_base=?,unit_price=?,total=?
                           WHERE id=?""",
                        (display_qty, new_base_qty, unit_price, line_total, row["line_id"]),
                    )

                conn.execute(
                    "UPDATE items SET opening_quantity=0,opening_unit_cost=0 WHERE id=?",
                    (current_item_id,),
                )
                conn.execute(
                    """DELETE FROM inventory_movements
                       WHERE item_id=? AND invoice_id IS NULL AND movement_type='adjustment'""",
                    (current_item_id,),
                )
                invoice_id = int(row["invoice_id"])
                affected_invoice_ids.add(invoice_id)
                reconciled_item_ids.add(current_item_id)
                updated_invoice_id = invoice_id

            for invoice_id in affected_invoice_ids:
                line_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM invoice_lines WHERE invoice_id=?",
                        (invoice_id,),
                    ).fetchone()[0]
                )
                if line_count == 0:
                    conn.execute("DELETE FROM inventory_movements WHERE invoice_id=?", (invoice_id,))
                    conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
                    conn.execute(
                        "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('delete','invoice',?,?)",
                        (invoice_id, "opening invoice emptied by balance update"),
                    )
                else:
                    invoice_total = float(
                        conn.execute(
                            "SELECT COALESCE(SUM(total),0) FROM invoice_lines WHERE invoice_id=?",
                            (invoice_id,),
                        ).fetchone()[0]
                    )
                    conn.execute(
                        """UPDATE invoices
                           SET total=?,initial_paid_amount=?,paid_amount=? WHERE id=?""",
                        (invoice_total, invoice_total, invoice_total, invoice_id),
                    )
                    conn.execute(
                        "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('update','invoice',?,?)",
                        (invoice_id, f"purchase:opening:{invoice_total}"),
                    )

            sql = (
                "SELECT id,name,opening_quantity,opening_unit_cost,purchase_price,base_unit_id "
                "FROM items WHERE item_type='مخزون' AND is_composite=0 AND opening_quantity > 1e-9 "
                "AND id NOT IN (SELECT DISTINCT item_id FROM invoice_lines WHERE item_id IS NOT NULL)"
            )
            params: list[object] = []
            if item_ids:
                filtered_ids = [int(i) for i in item_ids]
                sql += " AND id IN (" + ",".join("?" for _ in filtered_ids) + ")"
                params.extend(filtered_ids)
            candidates = [dict(r) for r in conn.execute(sql, params).fetchall()]
            candidates = [r for r in candidates if int(r["id"]) not in reconciled_item_ids]
            if not candidates and not affected_invoice_ids:
                return None

            invoice_id = updated_invoice_id
            if candidates:
                new_item_ids = [int(r["id"]) for r in candidates]
                conn.executemany(
                    "UPDATE items SET opening_quantity=0, opening_unit_cost=0 WHERE id=?",
                    [(i,) for i in new_item_ids],
                )
                for i in new_item_ids:
                    conn.execute(
                        "DELETE FROM inventory_movements "
                        "WHERE item_id=? AND invoice_id IS NULL AND movement_type='adjustment'",
                        (i,),
                    )

                lines = [
                    InvoiceLineInput(
                        description=f'{r["name"]} — أول المدة',
                        item_id=int(r["id"]),
                        unit_id=int(r["base_unit_id"]) if r["base_unit_id"] is not None else None,
                        quantity=float(r["opening_quantity"] or 0),
                        unit_price=float(r["opening_unit_cost"] or r["purchase_price"] or 0),
                    )
                    for r in candidates
                ]
                prepared, total, _paid = self._validate_and_prepare(
                    conn,
                    invoice_type="purchase",
                    lines=lines,
                    customer_id=None,
                    supplier_id=None,
                    paid_amount=0,
                )
                settings = {
                    str(r["key"]): str(r["value"])
                    for r in conn.execute("SELECT key,value FROM settings").fetchall()
                }
                print_snapshot = invoice_settings.encode_print_snapshot(
                    invoice_settings.print_snapshot_from_settings(settings)
                )
                cur = conn.execute(
                    """INSERT INTO invoices(type,customer_id,supplier_id,invoice_date,reference,notes,total,
                                             initial_paid_amount,paid_amount,allow_negative_stock,
                                             invoice_exchange_rate,invoice_currency_code,invoice_currency_symbol,
                                             print_options)
                       VALUES('purchase',NULL,NULL,?,?,?,?,?,?,0,?,?,?,?)""",
                    (
                        date.today().isoformat(),
                        (reference or "").strip() or "أول المدة",
                        OPENING_BALANCE_MARKER,
                        total,
                        total,
                        total,
                        currency.get_exchange_rate(settings),
                        currency.get_display_currency(settings),
                        currency.get_display_symbol(settings),
                        print_snapshot,
                    ),
                )
                invoice_id = int(cur.lastrowid)
                self._insert_lines(conn, invoice_id, prepared)
                conn.execute(
                    "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('create','invoice',?,?)",
                    (invoice_id, f"purchase:opening:{total}"),
                )

            AccountingRebuilder.rebuild(conn)
            invalidate_insights()
            return invoice_id

    def update_invoice(
        self,
        invoice_id: int,
        *,
        invoice_type: str,
        lines: list[InvoiceLineInput],
        customer_id: int | None = None,
        supplier_id: int | None = None,
        invoice_date: str | None = None,
        reference: str | None = None,
        notes: str | None = None,
        paid_amount: float = 0,
        allow_negative_stock: bool | None = None,
    ) -> None:
        inv_date = invoice_date or date.today().isoformat()
        with self.db.transaction() as conn:
            old = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if old is None:
                raise ValueError("الفاتورة غير موجودة")
            is_opening_invoice = OPENING_BALANCE_MARKER in str(old["notes"] or "")
            if is_opening_invoice and (
                invoice_type != "purchase" or customer_id is not None or supplier_id is not None
            ):
                raise ValueError("لا يمكن تغيير نوع أو جهة فاتورة «أول المدة»")
            before = self._snapshot(conn, invoice_id)
            prepared, total, paid_amount = self._validate_and_prepare(
                conn,
                invoice_type=invoice_type,
                lines=lines,
                customer_id=customer_id,
                supplier_id=supplier_id,
                paid_amount=paid_amount,
            )
            paid_amount = self._cash_paid_without_party(
                invoice_type, total, paid_amount, customer_id, supplier_id
            )
            self._require_party_for_credit(invoice_type, total, paid_amount, customer_id, supplier_id)
            if allow_negative_stock is None:
                allow_negative_stock = bool(old["allow_negative_stock"])
            updated_notes = (notes or "").strip()
            if (
                is_opening_invoice
                and OPENING_BALANCE_MARKER not in updated_notes
            ):
                updated_notes = f"{updated_notes} {OPENING_BALANCE_MARKER}".strip()
            conn.execute(
                """UPDATE invoices
                   SET type=?,customer_id=?,supplier_id=?,invoice_date=?,reference=?,notes=?,total=?,
                       initial_paid_amount=?,allow_negative_stock=?
                   WHERE id=?""",
                (
                    invoice_type,
                    customer_id,
                    supplier_id,
                    inv_date,
                    (reference or "").strip() or None,
                    updated_notes or None,
                    total,
                    paid_amount,
                    1 if (allow_negative_stock and invoice_type == "sale") else 0,
                    invoice_id,
                ),
            )
            conn.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (invoice_id,))
            self._insert_lines(conn, invoice_id, prepared)
            AccountingRebuilder.rebuild(conn)
            after = self._snapshot(conn, invoice_id)
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('update','invoice',?,?)",
                (invoice_id, f"{old['type']}:{old['total']} -> {invoice_type}:{total}"),
            )
        invalidate_insights()
        event_bus.publish(event_bus.INVOICE_UPDATED, {"invoice_id": invoice_id, "invoice_type": invoice_type, "total": total, "before": before, "after": after})

    def delete_invoice(self, invoice_id: int) -> None:
        with self.db.transaction() as conn:
            old = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if old is None:
                raise ValueError("الفاتورة غير موجودة")
            before = self._snapshot(conn, invoice_id)
            # inventory_movements uses SET NULL, so remove invoice movements first;
            # otherwise they would become fake manual adjustments after deletion.
            conn.execute("DELETE FROM inventory_movements WHERE invoice_id=?", (invoice_id,))
            conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
            AccountingRebuilder.rebuild(conn)
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('delete','invoice',?,?)",
                (invoice_id, f"{old['type']}:{old['total']}"),
            )
        invalidate_insights()
        event_bus.publish(event_bus.INVOICE_DELETED, {"invoice_id": invoice_id, "invoice_type": old["type"], "before": before})

    @staticmethod
    def _snapshot(conn, invoice_id: int) -> dict | None:
        """A serialisable before/after image of an invoice (for undo/redo)."""
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            return None
        lines = [
            dict(line)
            for line in conn.execute(
                "SELECT item_id,unit_id,quantity,unit_price,description,conversion_factor "
                "FROM invoice_lines WHERE invoice_id=? ORDER BY id",
                (invoice_id,),
            ).fetchall()
        ]
        return {
            "type": row["type"],
            "customer_id": row["customer_id"],
            "supplier_id": row["supplier_id"],
            "invoice_date": row["invoice_date"],
            "reference": row["reference"],
            "notes": row["notes"],
            "paid": float(row["initial_paid_amount"] or 0),
            "allow_negative": bool(row["allow_negative_stock"]),
            "lines": lines,
        }

    def _validate_and_prepare(
        self,
        conn,
        *,
        invoice_type: str,
        lines: list[InvoiceLineInput],
        customer_id: int | None,
        supplier_id: int | None,
        paid_amount: float,
    ) -> tuple[list[dict], float, float]:
        if invoice_type not in {"sale", "purchase"}:
            raise ValueError("نوع الفاتورة غير صحيح")
        if not lines:
            raise ValueError("يجب إضافة بند واحد على الأقل")
        if invoice_type == "sale" and supplier_id is not None:
            raise ValueError("فاتورة البيع لا ترتبط بمورد")
        if invoice_type == "purchase" and customer_id is not None:
            raise ValueError("فاتورة الشراء لا ترتبط بعميل")
        if customer_id is not None and conn.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise ValueError("العميل غير موجود")
        if supplier_id is not None and conn.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone() is None:
            raise ValueError("المورد غير موجود")

        paid = money.quantized(paid_amount or 0)
        if paid < -EPSILON:
            raise ValueError("المبلغ المدفوع غير صحيح")

        prepared: list[dict] = []
        total = Decimal(0)
        sum_qty = 0.0
        for raw in lines:
            qty = float(raw.quantity)
            price = money.quantized(raw.unit_price)
            if qty <= 0 or price < 0:
                raise ValueError("بيانات بند الفاتورة غير صحيحة")
            sum_qty += qty

            item = None
            factor = float(raw.conversion_factor or 1)
            description = (raw.description or "").strip()
            if raw.item_id is not None:
                item = conn.execute("SELECT * FROM items WHERE id=?", (raw.item_id,)).fetchone()
                if item is None:
                    raise ValueError("المادة غير موجودة")
                description = description or str(item["name"])
                factor = self._unit_factor(conn, item, raw.unit_id)
            elif factor <= 0:
                raise ValueError("معامل الوحدة غير صحيح")

            if not description:
                raise ValueError("وصف البند مطلوب")
            base_qty = qty * factor
            # Money discipline (core/money.py): every stored money value is
            # quantized to cents, half-up, so per-line totals and the running
            # invoice total never accumulate binary-float noise.
            line_total = money.quantized(qty * price)
            unit_cost = 0.0
            cost_amount = 0.0
            components: dict[int, float] | None = None
            is_composite = item is not None and int(item["is_composite"] or 0) == 1
            if is_composite:
                # A composite is a recipe, not stock: it is only ever sold
                # (never purchased) and its sale is exploded into the leaf
                # materials it consumes. The snapshot is frozen onto the line
                # so later recipe edits cannot rewrite this invoice's history.
                if invoice_type == "purchase":
                    raise ValueError(f"لا يمكن شراء مادة تجميعية: {item['name']} — تُصنع من موادها")
                from nano_offline.core import bom as _bom

                components = _bom.explode(conn, int(item["id"]), base_qty)
            # Services do not move inventory. Their purchase_price is the
            # standard cost per base unit and is snapshotted into the sale
            # invoice line so future price edits do not rewrite history.
            elif item is not None and item["item_type"] == "خدمة" and invoice_type == "sale":
                unit_cost = money.quantized(float(item["purchase_price"] or 0) * factor)
                cost_amount = money.quantized(unit_cost * qty)
            prepared.append(
                {
                    "item_id": raw.item_id,
                    "description": description,
                    "unit_id": raw.unit_id,
                    "conversion_factor": factor,
                    "quantity": qty,
                    "quantity_in_base": base_qty,
                    "unit_price": money.quantized(price),
                    "total": line_total,
                    "unit_cost": unit_cost,
                    "cost_amount": cost_amount,
                    "components": components,
                }
            )
            total += money.quantize(line_total)

        total_decimal = money.quantize(total)
        total_float = float(total_decimal)
        # A caller may derive the paid amount from the *display* total, which is
        # built from less-rounded numbers than the per-unit prices stored here
        # (6-decimal quantization). Allow for that rounding dust -- bounded from
        # the line quantities -- and snap a within-dust overpayment down to the
        # total, instead of rejecting a full payment for a rounding error.
        rounding_dust = (sum_qty + len(lines) + 3) * 0.5e-6 + EPSILON
        if paid > total_float + rounding_dust:
            raise ValueError("المبلغ المدفوع لا يمكن أن يتجاوز إجمالي الفاتورة")
        if abs(paid - total_float) <= rounding_dust:
            # Within rounding dust of the total -> treat as a full payment.
            paid = total_float
        paid = min(paid, total_float)
        return prepared, total_float, paid

    @staticmethod
    def _cash_paid_without_party(
        invoice_type: str,
        total: float,
        paid_amount: float,
        customer_id: int | None,
        supplier_id: int | None,
    ) -> float:
        """Anonymous sale/purchase is always a fully-settled cash invoice.

        This rule lives in the accounting service rather than only in the UI so
        imports, future integrations, and alternate screens cannot create an
        anonymous receivable/payable accidentally.
        """
        missing_party = (invoice_type == "sale" and customer_id is None) or (
            invoice_type == "purchase" and supplier_id is None
        )
        return float(total) if missing_party else float(paid_amount or 0)

    @staticmethod
    def _require_party_for_credit(
        invoice_type: str,
        total: float,
        paid_amount: float,
        customer_id: int | None,
        supplier_id: int | None,
    ) -> None:
        if total - paid_amount <= EPSILON:
            return
        if invoice_type == "sale" and customer_id is None:
            raise ValueError("اختر العميل للفاتورة الآجلة أو اجعل المدفوع مساويًا للإجمالي")
        if invoice_type == "purchase" and supplier_id is None:
            raise ValueError("اختر المورد للفاتورة الآجلة أو اجعل المدفوع مساويًا للإجمالي")

    @staticmethod
    def _ensure_restaurant_party(conn) -> int:
        row = conn.execute(
            "SELECT id FROM customers WHERE name=? COLLATE NOCASE",
            (restaurant_settings.DEFAULT_PARTY_NAME,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO customers(name) VALUES(?)",
            (restaurant_settings.DEFAULT_PARTY_NAME,),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _unit_factor(conn, item, unit_id: int | None) -> float:
        if unit_id is None:
            return 1.0
        base_unit_id = item["base_unit_id"]
        if base_unit_id is not None and int(base_unit_id) == int(unit_id):
            return 1.0
        row = conn.execute(
            "SELECT conversion_factor FROM item_units WHERE item_id=? AND unit_id=?",
            (item["id"], unit_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"الوحدة المحددة غير معرفة للمادة: {item['name']}")
        factor = float(row["conversion_factor"])
        if factor <= 0:
            raise ValueError("معامل الوحدة غير صحيح")
        return factor

    @staticmethod
    def _insert_lines(conn, invoice_id: int, prepared: list[dict]) -> None:
        for line in prepared:
            cur = conn.execute(
                """INSERT INTO invoice_lines(
                       invoice_id,item_id,description,unit_id,conversion_factor,quantity,
                       quantity_in_base,unit_price,total,unit_cost,cost_amount
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    invoice_id,
                    line["item_id"],
                    line["description"],
                    line["unit_id"],
                    line["conversion_factor"],
                    line["quantity"],
                    line["quantity_in_base"],
                    line["unit_price"],
                    line["total"],
                    line.get("unit_cost", 0.0),
                    line.get("cost_amount", 0.0),
                ),
            )
            components = line.get("components")
            if components:
                line_id = int(cur.lastrowid)
                conn.executemany(
                    "INSERT INTO invoice_line_components(invoice_line_id,component_item_id,base_qty) VALUES(?,?,?)",
                    [(line_id, int(cid), float(base_qty)) for cid, base_qty in components.items()],
                )

    def rebuild_derived_state(self) -> None:
        """Reconcile migrated databases and all derived balances/ledger rows."""
        with self.db.transaction() as conn:
            AccountingRebuilder.rebuild(conn)

    def open_invoices_for_party(self, party_type: str, party_id: int) -> list[dict]:
        if party_type not in {"customer", "supplier"}:
            raise ValueError("نوع الحساب غير صحيح")
        column = "customer_id" if party_type == "customer" else "supplier_id"
        invoice_type = "sale" if party_type == "customer" else "purchase"
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT id,type,invoice_date,reference,total,paid_amount,
                           (total-paid_amount) AS remaining_amount
                    FROM invoices
                    WHERE type=? AND {column}=? AND total-paid_amount>?
                    ORDER BY invoice_date,id""",
                (invoice_type, party_id, EPSILON),
            ).fetchall()
            return [dict(r) for r in rows]

    def open_sale_orders(self, limit: int = 200) -> list[dict]:
        """Unpaid sale invoices (restaurant preparation orders awaiting payment)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id,invoice_date,reference,total,paid_amount,
                          (total-paid_amount) AS remaining_amount
                   FROM invoices
                   WHERE type='sale' AND status='posted' AND (total-paid_amount)>?
                   ORDER BY invoice_date,id LIMIT ?""",
                (EPSILON, max(1, int(limit))),
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _payment_status(total: float, paid: float) -> str:
        if total - paid <= EPSILON:
            return "paid"
        if paid > EPSILON:
            return "partial"
        return "unpaid"
