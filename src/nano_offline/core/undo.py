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
# Idempotency key: the database path (stable per install), NOT id(ctx) -- object
# ids get reused after garbage collection, which would wrongly skip install.
_INSTALLED: set[str] = set()
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



def _voucher_kwargs(snapshot: dict) -> dict:
    allocs = {int(k): float(v) for k, v in (snapshot.get("allocations") or {}).items()}
    return dict(
        voucher_type=snapshot.get("voucher_type") or "receipt",
        amount=float(snapshot.get("amount") or 0),
        customer_id=snapshot.get("customer_id"),
        supplier_id=snapshot.get("supplier_id"),
        voucher_date=snapshot.get("voucher_date"),
        reference=snapshot.get("reference"),
        notes=snapshot.get("notes"),
        allocation_mode="manual" if allocs else "oldest",
        allocations=allocs or None,
    )


def _restore_voucher(ctx, snapshot: dict) -> int:
    return ctx.payments.create_voucher(**_voucher_kwargs(snapshot))


def _apply_voucher(ctx, voucher_id: int, snapshot: dict) -> None:
    ctx.payments.update_voucher(voucher_id, **_voucher_kwargs(snapshot))


def _apply_item(ctx, item_id: int, snapshot: dict) -> None:
    ctx.items.update(
        item_id,
        name=snapshot.get("name") or "",
        category_id=snapshot.get("category_id"),
        item_type=snapshot.get("item_type") or "مخزون",
        purchase_price=float(snapshot.get("purchase_price") or 0),
        selling_price=float(snapshot.get("selling_price") or 0),
        base_unit_id=snapshot.get("base_unit_id"),
        item_units=[
            {
                "unit_id": int(unit["unit_id"]),
                "conversion_factor": float(unit.get("conversion_factor") or 1),
                "purchase_price": unit.get("purchase_price"),
                "selling_price": unit.get("selling_price"),
            }
            for unit in snapshot.get("units") or []
        ],
        barcode=snapshot.get("barcode"),
    )
    # Sync secondary barcodes to the snapshot (best-effort).
    want = {(str(b.get("barcode") or ""), b.get("unit_id")) for b in snapshot.get("barcodes") or []}
    have = {int(b["id"]): (str(b.get("barcode") or ""), b.get("unit_id")) for b in ctx.items.list_barcodes(item_id)}
    for row_id, key in have.items():
        if key not in want:
            try:
                ctx.items.remove_barcode(row_id)
            except Exception:
                pass
    existing = set(have.values())
    for code, unit_id in want:
        if code and (code, unit_id) not in existing:
            try:
                ctx.items.add_barcode(item_id, code, unit_id=unit_id)
            except Exception:
                pass



def _item_snapshot(ctx, item_id: int) -> dict | None:
    item = ctx.items.get(item_id)
    if not item:
        return None
    units = [
        {
            "unit_id": int(unit["id"]),
            "conversion_factor": float(unit.get("conversion_factor") or 1),
            "purchase_price": unit.get("purchase_price"),
            "selling_price": unit.get("selling_price"),
        }
        for unit in ctx.items.units(item_id)
        if not unit.get("is_base")
    ]
    barcodes = [
        {"barcode": b.get("barcode"), "unit_id": b.get("unit_id")}
        for b in ctx.items.list_barcodes(item_id)
    ]
    return {
        "name": item.get("name") or "",
        "category_id": item.get("category_id"),
        "item_type": item.get("item_type") or "مخزون",
        "purchase_price": float(item.get("purchase_price") or 0),
        "selling_price": float(item.get("selling_price") or 0),
        "quantity": float(item.get("quantity") or 0),
        "base_unit_id": item.get("base_unit_id"),
        "barcode": item.get("barcode"),
        "units": units,
        "barcodes": barcodes,
    }


def _restore_item(ctx, snapshot: dict) -> int:
    item_id = ctx.items.create(
        name=snapshot.get("name") or "",
        category_id=snapshot.get("category_id"),
        item_type=snapshot.get("item_type") or "مخزون",
        purchase_price=float(snapshot.get("purchase_price") or 0),
        selling_price=float(snapshot.get("selling_price") or 0),
        quantity=float(snapshot.get("quantity") or 0),
        base_unit_id=snapshot.get("base_unit_id"),
        barcode=snapshot.get("barcode"),
        item_units=[
            {
                "unit_id": int(unit["unit_id"]),
                "conversion_factor": float(unit.get("conversion_factor") or 1),
                "purchase_price": unit.get("purchase_price"),
                "selling_price": unit.get("selling_price"),
            }
            for unit in snapshot.get("units") or []
        ],
    )
    for bar in snapshot.get("barcodes") or []:
        code = str(bar.get("barcode") or "").strip()
        if code:
            try:
                ctx.items.add_barcode(item_id, code, unit_id=bar.get("unit_id"))
            except Exception:
                pass
    movements = snapshot.get("movements") or []
    if movements:
        with ctx.db.transaction() as conn:
            conn.executemany(
                """INSERT INTO inventory_movements(
                       item_id,movement_type,quantity_delta,unit_cost,value_delta,movement_date,invoice_id
                   ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        item_id,
                        movement["movement_type"],
                        float(movement["quantity_delta"]),
                        float(movement.get("unit_cost") or 0),
                        float(movement.get("value_delta") or 0),
                        movement["movement_date"],
                        movement.get("invoice_id"),
                    )
                    for movement in movements
                ],
            )
    return item_id


def install(ctx) -> None:
    """Subscribe the invoice events for ``ctx`` (idempotent per database)."""
    if ctx is None:
        return
    try:
        key = str(ctx.db.path)
    except Exception:
        key = ""
    if not key:
        key = f"id{id(ctx)}"
    if key in _INSTALLED:
        return
    _INSTALLED.add(key)
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


    def on_voucher_created(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        snapshot = ctx.payments.get_voucher(int(payload["voucher_id"]))
        if not snapshot:
            return
        holder = {"id": int(payload["voucher_id"])}

        def undo_cmd() -> None:
            ctx.payments.delete_voucher(holder["id"])

        def redo_cmd() -> None:
            holder["id"] = _restore_voucher(ctx, snapshot)

        _MANAGER.record(undo_cmd, redo_cmd, label="إنشاء سند")

    def on_voucher_updated(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        voucher_id = int(payload["voucher_id"])
        before, after = payload.get("before"), payload.get("after")
        if not before or not after:
            return

        def undo_cmd() -> None:
            _apply_voucher(ctx, voucher_id, before)

        def redo_cmd() -> None:
            _apply_voucher(ctx, voucher_id, after)

        _MANAGER.record(undo_cmd, redo_cmd, label="تعديل سند")

    def on_voucher_deleted(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        before = payload.get("before")
        if not before:
            return
        holder = {"id": None}

        def undo_cmd() -> None:
            holder["id"] = _restore_voucher(ctx, before)

        def redo_cmd() -> None:
            if holder["id"] is not None:
                ctx.payments.delete_voucher(holder["id"])

        _MANAGER.record(undo_cmd, redo_cmd, label="حذف سند")

    def on_item_updated(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        item_id = int(payload["item_id"])
        before, after = payload.get("before"), payload.get("after")
        if not before or not after:
            return

        def undo_cmd() -> None:
            _apply_item(ctx, item_id, before)

        def redo_cmd() -> None:
            _apply_item(ctx, item_id, after)

        _MANAGER.record(undo_cmd, redo_cmd, label="تعديل مادة")


    def on_item_created(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        item_id = int(payload["item_id"])
        snapshot = _item_snapshot(ctx, item_id)
        if not snapshot:
            return
        # An item created with opening stock also gets an "أول المدة" invoice in
        # the bulk flow; reversing that safely needs opening-invoice handling,
        # so only record the (service/zero-stock) cases we can undo cleanly.
        if abs(float(snapshot.get("quantity") or 0)) > 1e-9:
            return
        holder = {"id": item_id}

        def undo_cmd() -> None:
            ctx.items.delete(holder["id"])

        def redo_cmd() -> None:
            holder["id"] = _restore_item(ctx, snapshot)

        _MANAGER.record(undo_cmd, redo_cmd, label="إنشاء مادة")

    def on_item_deleted(event: str, payload: dict) -> None:
        if _SUSPENDED["value"]:
            return
        before = payload.get("before")
        if not before:
            return
        holder = {"id": None}

        def undo_cmd() -> None:
            holder["id"] = _restore_item(ctx, before)

        def redo_cmd() -> None:
            if holder["id"] is not None:
                ctx.items.delete(holder["id"])

        _MANAGER.record(undo_cmd, redo_cmd, label="حذف مادة")

    bus.subscribe(event_bus.INVOICE_CREATED, on_created)
    bus.subscribe(event_bus.INVOICE_UPDATED, on_updated)
    bus.subscribe(event_bus.INVOICE_DELETED, on_deleted)
    bus.subscribe(event_bus.VOUCHER_CREATED, on_voucher_created)
    bus.subscribe(event_bus.VOUCHER_UPDATED, on_voucher_updated)
    bus.subscribe(event_bus.VOUCHER_DELETED, on_voucher_deleted)
    bus.subscribe(event_bus.ITEM_UPDATED, on_item_updated)
    bus.subscribe(event_bus.ITEM_CREATED, on_item_created)
    bus.subscribe(event_bus.ITEM_DELETED, on_item_deleted)


__all__ = ["UndoManager", "manager", "undo", "redo", "install"]