"""Persistent marker for whether derived accounting/inventory state is current.

``AppContext.create`` used to run the full derived-state rebuild on *every*
launch. That is invisible on a small database but blocks the UI for minutes on
a large one (a blank screen before the splash even paints). This module stores
a tiny marker in the existing open-ended ``settings`` table:

- ``derived_dirty``          -- "1" while a mutation may have left derived
  state stale; "0" once a rebuild has completed.
- ``derived_schema_version`` -- the schema version the last rebuild ran at, so
  a migration (or a restore/) forces exactly one rebuild afterwards.

A fresh/legacy database has neither key, so it is treated as dirty and rebuilt
once; from then on a clean shutdown skips the rebuild entirely.
"""

from __future__ import annotations

from nano_offline.core.database import SCHEMA_VERSION

DIRTY_KEY = "derived_dirty"
VERSION_KEY = "derived_schema_version"
SIGNATURE_KEY = "derived_signature"

#: Cheap fingerprint of the source-of-derived tables. A raw INSERT/UPDATE that
#: bypasses the services changes a count/sum and forces one rebuild, while a
#: clean shutdown matches and skips the rebuild entirely.
_SIGNATURE_SQL = """
SELECT
    (SELECT COUNT(*) FROM invoices),
    (SELECT COUNT(*) FROM invoice_lines),
    (SELECT COUNT(*) FROM payments),
    (SELECT COUNT(*) FROM vouchers),
    (SELECT COUNT(*) FROM expenses),
    (SELECT COUNT(*) FROM items),
    ROUND((SELECT COALESCE(SUM(opening_quantity),0) FROM items), 3),
    ROUND((SELECT COALESCE(SUM(opening_unit_cost),0) FROM items), 3)
"""


def _get(conn, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return str(row[0])


def _set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def signature(conn) -> str:
    try:
        row = conn.execute(_SIGNATURE_SQL).fetchone()
    except Exception:
        return ""
    return "|".join(str(v) for v in row) if row is not None else ""


def is_clean(db) -> bool:
    """True when derived state is known-current at the current schema version."""
    try:
        with db.connect() as conn:
            if _get(conn, DIRTY_KEY) != "0":
                return False
            if _get(conn, VERSION_KEY) != str(SCHEMA_VERSION):
                return False
            stored = _get(conn, SIGNATURE_KEY)
            return bool(stored) and stored == signature(conn)
    except Exception:
        return False


def mark_dirty_conn(conn) -> None:
    _set(conn, DIRTY_KEY, "1")


def mark_clean_conn(conn) -> None:
    _set(conn, DIRTY_KEY, "0")
    _set(conn, VERSION_KEY, str(SCHEMA_VERSION))
    _set(conn, SIGNATURE_KEY, signature(conn) or "")


def mark_dirty(db) -> None:
    with db.transaction() as conn:
        mark_dirty_conn(conn)


__all__ = [
    "DIRTY_KEY", "VERSION_KEY", "SIGNATURE_KEY",
    "is_clean", "signature", "mark_dirty", "mark_dirty_conn", "mark_clean_conn",
]
