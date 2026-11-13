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

    def delete_all(self) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM restaurant_tables")
            return int(cur.rowcount or 0)

    def create_many(
        self, *, area: str | None = None, start: int = 1, count: int = 10, prefix: str = ""
    ) -> int:
        """Create numbered tables ``prefix + start`` .. ``prefix + start+n-1``."""
        try:
            start = int(start)
            count = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError("قيم الإنشاء غير صحيحة") from exc
        if count < 1 or count > 500:
            raise ValueError("عدد الطاولات يجب أن يكون بين 1 و 500")
        area = (area or "").strip() or None
        prefix = (prefix or "").strip()
        created = 0
        with self.db.transaction() as conn:
            existing = {str(r["name"]) for r in conn.execute("SELECT name FROM restaurant_tables").fetchall()}
            for number in range(start, start + count):
                name = f"{prefix}{number}".strip()
                if not name or name in existing:
                    continue
                conn.execute(
                    "INSERT INTO restaurant_tables(name,area,sort_order,active) VALUES(?,?,?,1)",
                    (name, area, number),
                )
                existing.add(name)
                created += 1
        return created

    def reorder(self, table_id: int, target_id: int) -> None:
        """Move ``table_id`` into ``target_id``'s area, just before it."""
        if int(table_id) == int(target_id):
            return
        with self.db.transaction() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,area,sort_order FROM restaurant_tables ORDER BY area, sort_order, name"
                ).fetchall()
            ]
            by_id = {int(r["id"]): r for r in rows}
            dragged = by_id.get(int(table_id))
            target = by_id.get(int(target_id))
            if dragged is None or target is None:
                raise ValueError("طاولة غير موجودة")
            areas: dict = {}
            for row in rows:
                areas.setdefault(row["area"], []).append(int(row["id"]))
            for ids in areas.values():
                if int(table_id) in ids:
                    ids.remove(int(table_id))
            target_area = target["area"]
            target_list = areas.setdefault(target_area, [])
            index = target_list.index(int(target_id)) if int(target_id) in target_list else len(target_list)
            target_list.insert(index, int(table_id))
            for area, ids in areas.items():
                for order, tid in enumerate(ids):
                    conn.execute(
                        "UPDATE restaurant_tables SET area=?,sort_order=? WHERE id=?",
                        (area, order, tid),
                    )

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