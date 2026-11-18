from __future__ import annotations

import time

from nano_offline.core.asyncdb import Offloadable, blocking
from nano_offline.core.database import Database


class SettingsRepository(Offloadable):
    """Thin key/value accessor over the ``settings`` table.

    The schema already has an open-ended ``settings(key,value)`` table (used
    for ``currency``/``company_name``/etc.), so branding fields (logo,
    invoice accent color) live there too -- no schema migration needed.

    **Cached reads.** Money formatting reads several settings keys (display
    currency, exchange rate, symbols, precision) for *every* cell; without a
    cache that was one fresh SQLite connection + query per read -- thousands
    per screen rebuild (the "app gets slow with ~250 items" report). The whole
    table is tiny, so it is read once and served from a dict. ``set`` /
    ``set_many`` / ``clear`` update the cache in place, and a short TTL bounds
    staleness for the few places that write the table directly (license,
    day-close), so nothing has to thread invalidation through every caller.
    """

    #: Seconds before a lazily-refreshed reload (bounds staleness for direct
    #: SQL writers that bypass this repository).
    _CACHE_TTL = 2.0

    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, str] | None = None
        self._loaded_at = 0.0

    def _load(self) -> dict[str, str]:
        with self.db.connect() as conn:
            return {str(r["key"]): str(r["value"]) for r in conn.execute("SELECT key,value FROM settings").fetchall()}

    def _snapshot(self) -> dict[str, str]:
        now = time.monotonic()
        if self._cache is None or (now - self._loaded_at) > self._CACHE_TTL:
            self._cache = self._load()
            self._loaded_at = now
        return self._cache

    def invalidate(self) -> None:
        """Force the next read to reload the settings table."""
        self._cache = None

    def _apply_local(self, key: str, value: str | None) -> None:
        # Keep the cache authoritative after a write through this repo so the
        # common "set then immediately read" path never reloads or goes stale.
        if self._cache is None:
            return
        if value is None or value == "":
            self._cache.pop(str(key), None)
        else:
            self._cache[str(key)] = str(value)
        self._loaded_at = time.monotonic()

    @blocking
    def get_all(self) -> dict[str, str]:
        return dict(self._snapshot())

    @blocking
    def get(self, key: str, default: str = "") -> str:
        return self._snapshot().get(str(key), default)

    @blocking
    def set(self, key: str, value: str | None) -> None:
        with self.db.transaction() as conn:
            if value is None or value == "":
                conn.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )
        self._apply_local(key, None if value is None else str(value))

    @blocking
    def set_many(self, values: dict[str, str | None]) -> None:
        with self.db.transaction() as conn:
            for key, value in values.items():
                if value is None or value == "":
                    conn.execute("DELETE FROM settings WHERE key=?", (key,))
                else:
                    conn.execute(
                        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)),
                    )
        for key, value in values.items():
            self._apply_local(key, None if value is None else str(value))

    @blocking
    def clear(self) -> int:
        """Delete every settings row (factory defaults; business data untouched)."""
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM settings")
            count = int(cur.rowcount or 0)
        # Defaults fall back to code constants; cache an empty snapshot so the
        # next read of any key returns its default without a query.
        self._cache = {}
        self._loaded_at = time.monotonic()
        return count


__all__ = ["SettingsRepository"]
