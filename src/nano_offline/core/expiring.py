"""Tiny thread-safe TTL cache for expensive derived/analytics results.

The dashboard's "smart" block (owner pulse, assistant decisions, the
alerts rules engine) costs >1s of SQLite+rules work on a mid-size shop
(measured) -- and it used to be recomputed from scratch on *every*
navigation to the dashboard. These are advisory insights, not live
balances: a short cache is the difference between a snappy tab switch
and a frozen UI, with no correctness cost to the KPIs themselves (those
stay uncached).

Design notes:

* ``time.monotonic`` for freshness (immune to wall-clock changes).
* Values are computed *outside* the lock -- a slow producer must never
  block cache reads from other threads (the alerts sync runs on a
  worker thread).
* Invalidation is all-or-nothing on purpose: the cached keys ("alerts",
  "owner_pulse", ...) are cheap to recompute together after a write, and
  a coarse reset can't leave stale siblings behind.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = ["ExpiringCache", "cached_insight", "invalidate_insights"]


class ExpiringCache:
    __slots__ = ("_lock", "_store")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get_or_compute(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and now < hit[0]:
                return hit[1]
        value = fn()
        with self._lock:
            self._store[key] = (time.monotonic() + max(0.0, float(ttl)), value)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._store.clear()


SMART_CACHE = ExpiringCache()


def cached_insight(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    """Return ``fn()`` result, reusing a value computed within ``ttl`` seconds."""
    return SMART_CACHE.get_or_compute(key, ttl, fn)


def invalidate_insights() -> None:
    """Drop every cached insight so the next dashboard visit recomputes.

    Called after data-changing flows (sales, vouchers, expenses, restore)
    so a user who just recorded something immediately sees fresh advice.
    """
    SMART_CACHE.invalidate()
