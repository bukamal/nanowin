"""Restaurant dining tables (with optional areas/floors)."""

from __future__ import annotations

from nano_offline.core.database import Database


class TableRepository:
    def __init__(self, db: Database):
        self.db = db

    def list(self, *, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM restaurant_tables"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY area, sort_order, name"
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def get(self, table_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM restaurant_tables WHERE id=?", (int(table_id),)).fetchone()
            return dict(row) if row else None

    def create(
        self,
        *,
        name: str,
        area: str | None = None,
        sort_order: int = 0,
        active: bool = True,
    ) -> int:
        name, area, order = self._validate(name, area, sort_order)
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO restaurant_tables(name,area,sort_order,active) VALUES(?,?,?,?)",
                (name, area, order, 1 if active else 0),
            )
            return int(cur.lastrowid)

    def update(
        self,
        table_id: int,
        *,
        name: str,
        area: str | None = None,
        sort_order: int = 0,
        active: bool = True,
    ) -> None:
        name, area, order = self._validate(name, area, sort_order)
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM restaurant_tables WHERE id=?", (int(table_id),)).fetchone() is None:
                raise ValueError("الطاولة غير موجودة")
            conn.execute(
                "UPDATE restaurant_tables SET name=?,area=?,sort_order=?,active=? WHERE id=?",
                (name, area, order, 1 if active else 0, int(table_id)),
            )

    def delete(self, table_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM restaurant_tables WHERE id=?", (int(table_id),))

    @staticmethod
    def _validate(name: str, area: str | None, sort_order: int) -> tuple[str, str | None, int]:
        name = (name or "").strip()
        if not name:
            raise ValueError("اسم الطاولة مطلوب")
        try:
            order = int(sort_order or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("ترتيب الطاولة غير صحيح") from exc
        return name, (area or "").strip() or None, order