"""Sections (print stations) + their network printer config and print queue.

A section is a department (kitchen/bar/...) whose items' station tickets print
on one network ESC/POS printer. Items resolve to a section via
``items.section_id`` first, then their category's ``section_id``.
"""

from __future__ import annotations

from nano_offline.core.database import Database

_KINDS = {"kitchen", "receipt"}


class SectionRepository:
    def __init__(self, db: Database):
        self.db = db

    def list(self, *, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM sections"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY sort_order, name"
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def get(self, section_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sections WHERE id=?", (int(section_id),)).fetchone()
            return dict(row) if row else None

    def create(
        self,
        *,
        name: str,
        kind: str = "kitchen",
        printer_host: str | None = None,
        printer_port: int = 9100,
        paper_width_mm: int = 80,
        copies: int = 1,
        encoding: str = "cp1256",
        active: bool = True,
        sort_order: int = 0,
    ) -> int:
        values = self._validate(
            name=name, kind=kind, printer_host=printer_host, printer_port=printer_port,
            paper_width_mm=paper_width_mm, copies=copies, encoding=encoding, active=active,
            sort_order=sort_order,
        )
        with self.db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO sections(
                       name,kind,printer_host,printer_port,paper_width_mm,copies,encoding,active,sort_order
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    values["name"], values["kind"], values["printer_host"], values["printer_port"],
                    values["paper_width_mm"], values["copies"], values["encoding"],
                    1 if values["active"] else 0, values["sort_order"],
                ),
            )
            section_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('create','section',?,?)",
                (section_id, values["name"]),
            )
            return section_id

    def update(
        self,
        section_id: int,
        *,
        name: str,
        kind: str = "kitchen",
        printer_host: str | None = None,
        printer_port: int = 9100,
        paper_width_mm: int = 80,
        copies: int = 1,
        encoding: str = "cp1256",
        active: bool = True,
        sort_order: int = 0,
    ) -> None:
        values = self._validate(
            name=name, kind=kind, printer_host=printer_host, printer_port=printer_port,
            paper_width_mm=paper_width_mm, copies=copies, encoding=encoding, active=active,
            sort_order=sort_order,
        )
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM sections WHERE id=?", (int(section_id),)).fetchone() is None:
                raise ValueError("القسم غير موجود")
            conn.execute(
                """UPDATE sections SET name=?,kind=?,printer_host=?,printer_port=?,paper_width_mm=?,
                       copies=?,encoding=?,active=?,sort_order=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    values["name"], values["kind"], values["printer_host"], values["printer_port"],
                    values["paper_width_mm"], values["copies"], values["encoding"],
                    1 if values["active"] else 0, values["sort_order"], int(section_id),
                ),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('update','section',?,?)",
                (int(section_id), values["name"]),
            )

    def delete(self, section_id: int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT name FROM sections WHERE id=?", (int(section_id),)).fetchone()
            if row is None:
                raise ValueError("القسم غير موجود")
            # Foreign keys set items/categories/print_jobs section_id to NULL.
            conn.execute("DELETE FROM sections WHERE id=?", (int(section_id),))
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('delete','section',?,?)",
                (int(section_id), str(row["name"])),
            )

    # ---- assignment ---------------------------------------------------- #

    def set_item_section(self, item_id: int, section_id: int | None) -> None:
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM items WHERE id=?", (int(item_id),)).fetchone() is None:
                raise ValueError("المادة غير موجودة")
            conn.execute(
                "UPDATE items SET section_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(section_id) if section_id else None, int(item_id)),
            )

    def set_category_section(self, category_id: int, section_id: int | None) -> None:
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM categories WHERE id=?", (int(category_id),)).fetchone() is None:
                raise ValueError("التصنيف غير موجود")
            conn.execute(
                "UPDATE categories SET section_id=? WHERE id=?",
                (int(section_id) if section_id else None, int(category_id)),
            )

    def invoice_section_lines(self, invoice_id: int) -> dict[int, list[dict]]:
        """Group a sale invoice's lines by resolved section id.

        Resolution order: item override, then category default. Lines with no
        resolved section are omitted (they go on no station ticket).
        """
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT il.id AS line_id, il.quantity, il.description,
                          it.name AS item_name,
                          COALESCE(it.section_id, c.section_id) AS section_id
                   FROM invoice_lines il
                   LEFT JOIN items it ON it.id=il.item_id
                   LEFT JOIN categories c ON c.id=it.category_id
                   WHERE il.invoice_id=?""",
                (int(invoice_id),),
            ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            section_id = row["section_id"]
            if section_id is None:
                continue
            grouped.setdefault(int(section_id), []).append(
                {
                    "name": str(row["item_name"] or row["description"] or ""),
                    "quantity": float(row["quantity"] or 0),
                }
            )
        return grouped

    # ---- print queue --------------------------------------------------- #

    def enqueue_job(
        self,
        *,
        invoice_id: int,
        section_id: int | None,
        printer_host: str,
        printer_port: int,
        payload: bytes,
        dedupe_key: str,
    ) -> bool:
        """Insert one pending job; returns False when it already existed."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO print_jobs(
                       invoice_id,section_id,printer_host,printer_port,payload,dedupe_key
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    int(invoice_id),
                    int(section_id) if section_id else None,
                    str(printer_host),
                    int(printer_port or 9100),
                    bytes(payload),
                    str(dedupe_key),
                ),
            )
            return cur.rowcount > 0

    def pending_jobs(self, limit: int = 50) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM print_jobs WHERE status='pending' ORDER BY id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_sent(self, job_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE print_jobs SET status='sent',attempts=attempts+1,error=NULL,"
                "sent_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(job_id),),
            )

    def mark_failed(self, job_id: int, error: str, *, final: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE print_jobs SET status=?,attempts=attempts+1,error=? WHERE id=?",
                ("failed" if final else "pending", str(error)[:500], int(job_id)),
            )

    def retry_failed(self) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE print_jobs SET status='pending',attempts=0 WHERE status='failed'")
            return int(cur.rowcount or 0)

    def delete_jobs_for_invoice(self, invoice_id: int) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM print_jobs WHERE invoice_id=?", (int(invoice_id),))
            return int(cur.rowcount or 0)

    def sales_by_section(
        self, *, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict]:
        """Revenue/COGS/profit per section over posted sale invoices.

        Lines resolve with the same rule as printing (item override, then
        category default); unassigned items are grouped under a NULL section
        and labelled by the caller.
        """
        clauses = ["inv.type='sale'", "inv.status='posted'"]
        params: list[object] = []
        if date_from:
            clauses.append("inv.invoice_date >= ?")
            params.append(str(date_from))
        if date_to:
            clauses.append("inv.invoice_date <= ?")
            params.append(str(date_to))
        where = " AND ".join(clauses)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT s.id AS section_id, s.name AS section_name,
                           COALESCE(SUM(il.total),0) AS revenue,
                           COALESCE(SUM(il.cost_amount),0) AS cost,
                           COALESCE(SUM(il.quantity),0) AS quantity,
                           COUNT(DISTINCT inv.id) AS invoices
                    FROM invoice_lines il
                    JOIN invoices inv ON inv.id=il.invoice_id
                    LEFT JOIN items it ON it.id=il.item_id
                    LEFT JOIN categories c ON c.id=it.category_id
                    LEFT JOIN sections s ON s.id=COALESCE(it.section_id, c.section_id)
                    WHERE {where}
                    GROUP BY s.id, s.name
                    ORDER BY revenue DESC""",
                params,
            ).fetchall()
        result = []
        for row in rows:
            revenue = float(row["revenue"] or 0)
            cost = float(row["cost"] or 0)
            result.append(
                {
                    "section_id": row["section_id"],
                    "section_name": row["section_name"],
                    "revenue": revenue,
                    "cost": cost,
                    "profit": revenue - cost,
                    "quantity": float(row["quantity"] or 0),
                    "invoices": int(row["invoices"] or 0),
                }
            )
        return result

    def counts(self) -> dict:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS c FROM print_jobs GROUP BY status").fetchall()
        result = {"pending": 0, "sent": 0, "failed": 0}
        for row in rows:
            result[str(row["status"])] = int(row["c"])
        return result

    @staticmethod
    def _validate(
        *,
        name: str,
        kind: str,
        printer_host: str | None,
        printer_port: int,
        paper_width_mm: int,
        copies: int,
        encoding: str,
        active: bool,
        sort_order: int,
    ) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("اسم القسم مطلوب")
        kind = (kind or "kitchen").strip().lower()
        if kind not in _KINDS:
            raise ValueError("نوع القسم غير صحيح")
        width = int(paper_width_mm or 80)
        if width not in (58, 80):
            raise ValueError("عرض الورق يجب أن يكون 58 أو 80 مم")
        try:
            port = int(printer_port or 9100)
            count = int(copies or 1)
            order = int(sort_order or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("بيانات الطابعة غير صحيحة") from exc
        if not (1 <= port <= 65535):
            raise ValueError("منفذ الطابعة غير صحيح")
        if count < 1:
            raise ValueError("عدد النسخ يجب أن يكون 1 على الأقل")
        return {
            "name": name,
            "kind": kind,
            "printer_host": (printer_host or "").strip() or None,
            "printer_port": port,
            "paper_width_mm": width,
            "copies": count,
            "encoding": (encoding or "cp1256").strip().lower() or "cp1256",
            "active": bool(active),
            "sort_order": order,
        }