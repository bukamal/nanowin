"""A tiny, dependency-free publish/subscribe bus for domain events.

Central place for the app to react to *what happened* (an invoice was saved,
an item changed, a voucher was deleted) instead of every caller wiring its own
side effects. Subscribers are best-effort: a listener that raises never breaks
the business operation that published the event.

Events are plain strings (see the ``*_CREATED`` constants). Payloads are dicts
with ids/type, deliberately small -- a subscriber that needs more can re-read
it from its own context.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable

# --- event names -------------------------------------------------------- #
INVOICE_CREATED = "invoice.created"
INVOICE_UPDATED = "invoice.updated"
INVOICE_DELETED = "invoice.deleted"

ITEM_CREATED = "item.created"
ITEM_UPDATED = "item.updated"
ITEM_DELETED = "item.deleted"

PARTY_CREATED = "party.created"
PARTY_UPDATED = "party.updated"
PARTY_DELETED = "party.deleted"

VOUCHER_CREATED = "voucher.created"
VOUCHER_UPDATED = "voucher.updated"
VOUCHER_DELETED = "voucher.deleted"

EXPENSE_CREATED = "expense.created"
EXPENSE_UPDATED = "expense.updated"
EXPENSE_DELETED = "expense.deleted"

#: Every event that means "read caches may now be stale".
MUTATION_EVENTS: frozenset[str] = frozenset({
    INVOICE_CREATED, INVOICE_UPDATED, INVOICE_DELETED,
    ITEM_CREATED, ITEM_UPDATED, ITEM_DELETED,
    PARTY_CREATED, PARTY_UPDATED, PARTY_DELETED,
    VOUCHER_CREATED, VOUCHER_UPDATED, VOUCHER_DELETED,
    EXPENSE_CREATED, EXPENSE_UPDATED, EXPENSE_DELETED,
})

Listener = Callable[[str, dict[str, Any]], None]


class EventBus:
    """A minimal thread-safe event bus."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event: str, listener: Listener) -> Callable[[], None]:
        """Register ``listener``; returns an unsubscribe callable."""
        with self._lock:
            self._listeners[event].append(listener)
        return lambda: self.unsubscribe(event, listener)

    def unsubscribe(self, event: str, listener: Listener) -> None:
        with self._lock:
            handlers = self._listeners.get(event)
            if not handlers:
                return
            try:
                handlers.remove(listener)
            except ValueError:
                pass
            if not handlers:
                self._listeners.pop(event, None)

    def publish(self, event: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        with self._lock:
            handlers = list(self._listeners.get(event, ()))
        for handler in handlers:
            try:
                handler(event, payload)
            except Exception:
                logging.getLogger(__name__).exception("event handler failed for %s", event)

    def clear(self) -> None:
        with self._lock:
            self._listeners.clear()


_BUS = EventBus()


def bus() -> EventBus:
    return _BUS


def publish(event: str, payload: dict[str, Any] | None = None) -> None:
    _BUS.publish(event, payload)


__all__ = [
    "EventBus", "bus", "publish", "MUTATION_EVENTS",
    "INVOICE_CREATED", "INVOICE_UPDATED", "INVOICE_DELETED",
    "ITEM_CREATED", "ITEM_UPDATED", "ITEM_DELETED",
    "PARTY_CREATED", "PARTY_UPDATED", "PARTY_DELETED",
    "VOUCHER_CREATED", "VOUCHER_UPDATED", "VOUCHER_DELETED",
    "EXPENSE_CREATED", "EXPENSE_UPDATED", "EXPENSE_DELETED",
]