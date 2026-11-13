from __future__ import annotations

from nano_offline.core.asyncdb import Offloadable, blocking
from nano_offline.core.database import Database
from nano_offline.core import event_bus
from nano_offline.core import search_index


class PartyRepository(Offloadable):
    def __init__(self, db: Database, table: str):
        if table not in {"customers", "suppliers"}:
            raise ValueError(table)
        self.db = db
        self.table = table

    @property
    def party_type(self) -> str:
        return "customer" if self.table == "customers" else "supplier"

    @blocking
    def list(
        self,
        search: str = "",
        limit: int | None = None,
        offset: int = 0,
        *,
        include_system: bool = False,
    ) -> list[dict]:
        """Parties filtered by substring name, with optional paging.

        System parties (e.g. the restaurant "طلبات مفتوحة" open-orders account)
        are hidden unless ``include_system`` is set -- they exist for
        accounting, not for the user to pick or browse.
        """
        with self.db.connect() as conn:
            query = search.strip()
            fts_table = f"{self.table}_fts"
            use_fts = search_index.is_fts_eligible(conn, fts_table, query)
            hide_system = self.table == "customers" and not include_system
            sql = f"SELECT {self.table}.* FROM {self.table}"
            params: list = []
            if use_fts:
                sql += f" JOIN {fts_table} ON {fts_table}.rowid = {self.table}.id"
            conditions: list[str] = []
            if query:
                if use_fts:
                    conditions.append(f"{fts_table} MATCH ?")
                    params.append(search_index.match_expression(query))
                else:
                    conditions.append("name LIKE ? ESCAPE '\\'")
                    params.append(search_index.like_pattern(query))
            if hide_system:
                conditions.append(f"{self.table}.is_system=0")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY name"
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([int(limit), int(offset)])
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            except Exception:
                if not use_fts:
                    raise
                # FTS path unavailable at runtime; retry with the LIKE fallback.
                like_conditions: list[str] = []
                like_params: list = []
                if query:
                    like_conditions.append("name LIKE ? ESCAPE '\\'")
                    like_params.append(search_index.like_pattern(query))
                if hide_system:
                    like_conditions.append(f"{self.table}.is_system=0")
                like = f"SELECT {self.table}.* FROM {self.table}"
                if like_conditions:
                    like += " WHERE " + " AND ".join(like_conditions)
                like += " ORDER BY name"
                if limit is not None:
                    like += " LIMIT ? OFFSET ?"
                    like_params.extend([int(limit), int(offset)])
                return [dict(r) for r in conn.execute(like, like_params).fetchall()]

    @blocking
    def count(self, search: str = "", *, include_system: bool = False) -> int:
        """Total parties matching ``search`` (literal name substring)."""
        query = search.strip()
        hide_system = self.table == "customers" and not include_system
        with self.db.connect() as conn:
            if query:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {self.table} WHERE name LIKE ? ESCAPE '\\'"
                    + (" AND is_system=0" if hide_system else ""),
                    (search_index.like_pattern(query),),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {self.table}" + (" WHERE is_system=0" if hide_system else "")
                ).fetchone()
            return int(row[0])

    @blocking
    def get(self, party_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(f"SELECT * FROM {self.table} WHERE id=?", (party_id,)).fetchone()
            return dict(row) if row else None

    @blocking
    def activity_summary(self, party_id: int) -> dict:
        party = self.get(party_id)
        if party is None:
            raise ValueError("الحساب غير موجود")
        id_column = "customer_id" if self.table == "customers" else "supplier_id"
        invoice_type = "sale" if self.table == "customers" else "purchase"
        with self.db.connect() as conn:
            stats = conn.execute(
                f"""SELECT COUNT(*) AS invoice_count, COALESCE(SUM(total),0) AS invoice_total,
                           COALESCE(SUM(paid_amount),0) AS paid_total,
                           COALESCE(SUM(MAX(total-paid_amount,0)),0) AS outstanding_total,
                           MAX(invoice_date) AS last_invoice_date
                    FROM invoices WHERE type=? AND {id_column}=? AND status='posted'""",
                (invoice_type, party_id),
            ).fetchone()
            recent = conn.execute(
                f"""SELECT id,type,invoice_date,reference,total,paid_amount,
                           MAX(total-paid_amount,0) AS remaining_amount
                    FROM invoices WHERE type=? AND {id_column}=? AND status='posted'
                    ORDER BY invoice_date DESC,id DESC LIMIT 8""",
                (invoice_type, party_id),
            ).fetchall()
        result = dict(party)
        result.update(dict(stats))
        result["recent_invoices"] = [dict(r) for r in recent]
        return result

    @blocking
    def create(self, name: str, phone: str | None = None, address: str | None = None) -> int:
        name = name.strip()
        if not name:
            raise ValueError("الاسم مطلوب")
        with self.db.transaction() as conn:
            try:
                cur = conn.execute(
                    f"INSERT INTO {self.table}(name,phone,address) VALUES(?,?,?)",
                    (name, phone or None, address or None),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError("يوجد سجل بنفس الاسم") from exc
                raise
            party_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('create',?,?,?)",
                (self.table[:-1], party_id, name),
            )
        event_bus.publish(event_bus.PARTY_CREATED, {"party_type": self.party_type, "party_id": party_id, "name": name})
        return party_id

    @blocking
    def update(self, party_id: int, name: str, phone: str | None = None, address: str | None = None) -> None:
        name = name.strip()
        if not name:
            raise ValueError("الاسم مطلوب")
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {self.table} SET name=?,phone=?,address=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, phone or None, address or None, party_id),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('update',?,?,?)",
                (self.table[:-1], party_id, name),
            )
        event_bus.publish(event_bus.PARTY_UPDATED, {"party_type": self.party_type, "party_id": party_id, "name": name})

    @blocking
    def delete(self, party_id: int) -> None:
        with self.db.transaction() as conn:
            related = "customer_id" if self.table == "customers" else "supplier_id"
            invoice = conn.execute(f"SELECT 1 FROM invoices WHERE {related}=? LIMIT 1", (party_id,)).fetchone()
            payment = conn.execute(f"SELECT 1 FROM payments WHERE {related}=? LIMIT 1", (party_id,)).fetchone()
            if invoice or payment:
                raise ValueError("لا يمكن الحذف لوجود حركات مالية مرتبطة")
            conn.execute(f"DELETE FROM {self.table} WHERE id=?", (party_id,))
        event_bus.publish(event_bus.PARTY_DELETED, {"party_type": self.party_type, "party_id": party_id})
