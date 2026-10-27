from __future__ import annotations

"""Service-layer access to the ``audit_log`` table for UI actions.

Views used to run raw ``INSERT INTO audit_log`` SQL (invoice/pos print
logging). Routing it through a service keeps every data operation behind the
service layer -- a prerequisite for the future client/server transport, where
a view must never touch ``ctx.db`` directly.
"""

from nano_offline.core.asyncdb import Offloadable, blocking
from nano_offline.core.database import Database


class AuditService(Offloadable):
    """Audit-log writers/readers that the UI can call safely."""

    def __init__(self, db: Database):
        self.db = db

    @blocking
    def log_print(
        self,
        *,
        entity_type: str,
        entity_id: int,
        channel: str = "print",
        user_id: int | None = None,
        username: str | None = None,
    ) -> int:
        """Record a reprint and return how many times this entity was printed.

        The count lets the UI tell the cashier "طُبعت (مرة رقم N)" without a
        second query.
        """
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO audit_log(action,entity_type,entity_id,details,user_id,username)
                   VALUES('print',?,?,?,?,?)""",
                (str(entity_type), int(entity_id), str(channel), user_id, username),
            )
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='print' AND entity_type=? AND entity_id=?",
                (str(entity_type), int(entity_id)),
            ).fetchone()
            return int(row[0] if row is not None else 0)


__all__ = ["AuditService"]
