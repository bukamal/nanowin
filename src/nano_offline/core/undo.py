"""Session-level Undo/Redo driven by the domain event bus.

A small command/memento manager: services publish what happened (with enough
snapshot data to reverse it) and :func:`install` records matching undo/redo
commands on the global :class:`UndoManager`. Undo/redo run best-effort --
a failing command is reported, never raised into the caller that triggered it.

Scope today: **invoice** create/update/delete (the highest-value, cleanly
reversible case). Items/vouchers follow the same pattern (publish a snapshot,
record a pair of commands).
"""

from __future__ import annotations

import logging
from typing import Callable

from nano_offline.core import event_bus

log = logging.getLogger(__name__)


class UndoManager:
    def __init__(self, *, limit: int = 50) -> None:
        self._undo: list[tuple[Callable[[], None], Callable[[], None], str]] = []
        self._redo: list[tuple[Callable[[], None], Callable[[], None], str]] = []
        self._limit = limit

    def record(self, undo: Callable[[], None], redo: Callable[[], None], label: str = "") -> None:
        self._undo.append((undo, redo, label))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> str | None:
        if not self._undo:
            return None
        undo, redo, label = self._undo.pop()
        _SUSPENDED["value"] = True
        try:
            undo()
        except Exception:
            log.exception("undo failed: %s", label)
            return None
        finally:
            _SUSPENDED["value"] = False
        self._redo.append((undo, redo, label))
        return label

    def redo(self) -> str | None:
        if not self._redo:
            return None
        undo, redo, label = self._redo.pop()
        _SUSPENDED["value"] = True
        try:
            redo()
        except Exception:
            log.exception("redo failed: %s", label)
            return None
        finally:
            _SUSPENDED["value"] = False
        self._undo.append((undo, redo, label))
        return label

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


_MANAGER = UndoManager()
_INSTALLED: set[int] = set()
# While undo/redo runs it re-publishes domain events; don't record those.
_SUSPENDED = {"value": False}


def manager() -> UndoManager:
    return _MANAGER


def undo() -> str | None:
    return _MANAGER.undo()


def redo() -> str | None:
    return _MANAGER.redo()


def _invoice_kwargs(snapshot: dict) -> dict:
    from nano_offline.services.invoice_service import InvoiceLineInput

    lines = [
        InvoiceLineInput(
            description=str(line.get("description") or ""),
            item_id=int(line["item_id"]) if line.get("item_id") else None,
            unit_id=int(line["unit_id"]) if line.get("unit_id") else None,
            conversion_factor=float(line.get("conversion_factor") or 1),
            quantity=float(line.get("quantity") or 0),
            unit_price=float(line.get("unit_price") or 0),
        )
        for line in snapshot.get("lines") or []
    ]
    return dict(
        invoice_type=snapshot.get("type") or "sale",
        lines=lines,
        customer_id=snapshot.get("customer_id"),
        supplier_id=snapshot.get("supplier_id"),
        invoice_date=snapshot.get("invoice_date"),
        reference=snapshot.get("reference"),
        notes=snapshot.get("notes"),
        paid_amount=float(snapshot.get("paid") or 0),
        allow_negative_stock=bool(snapshot.get("allow_negative")) or None,
    )


def _restore_invoice(ctx, snapshot: dict) -> int:
    return ctx.invoices.create_invoice(**_invoice_kwargs(snapshot))


def _apply_invoice(ctx, invoice_id: int, snapshot: dict) -> None:
    ctx.invoices.update_invoice(invoice_id, **_invoice_kwargs(snapshot))


def install(ctx) -> None:
    """Subscribe the invoice events for ``ctx`` (idempotent per context)."""
    if ctx is None or id(ctx) in _INSTALLED:
        return
    _INSTALLED.add(id(ctx))
    bus = event_bus.bus()

    def on_created(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        snapshot = ctx.invoices.get_invoice(int(payload["invoice_id"]))
        if not snapshot:
            return
        holder = {"id": int(payload["invoice_id"])}

        def undo_cmd() -> None:
            ctx.invoices.delete_invoice(holder["id"])

        def redo_cmd() -> None:
            holder["id"] = _restore_invoice(ctx, snapshot)

        _MANAGER.record(undo_cmd, redo_cmd, label="إنشاء فاتورة")

    def on_updated(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        invoice_id = int(payload["invoice_id"])
        before, after = payload.get("before"), payload.get("after")
        if not before or not after:
            return

        def undo_cmd() -> None:
            _apply_invoice(ctx, invoice_id, before)

        def redo_cmd() -> None:
            _apply_invoice(ctx, invoice_id, after)

        _MANAGER.record(undo_cmd, redo_cmd, label="تعديل فاتورة")

    def on_deleted(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        before = payload.get("before")
        if not before:
            return
        holder = {"id": None}

        def undo_cmd() -> None:
            holder["id"] = _restore_invoice(ctx, before)

        def redo_cmd() -> None:
            if holder["id"] is not None:
                ctx.invoices.delete_invoice(holder["id"])

        _MANAGER.record(undo_cmd, redo_cmd, label="حذف فاتورة")

    bus.subscribe(event_bus.INVOICE_CREATED, on_created)
    bus.subscribe(event_bus.INVOICE_UPDATED, on_updated)
    bus.subscribe(event_bus.INVOICE_DELETED, on_deleted)


__all__ = ["UndoManager", "manager", "undo", "redo", "install"]