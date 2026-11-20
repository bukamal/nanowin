from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

import flet as ft
import pytest

from nano_offline.app_context import AppContext
from nano_offline.core.cancellation import OperationCancelled
from nano_offline.components.restore_flow import RestoreFlow
from nano_offline.services.invoice_service import InvoiceLineInput

PASSWORD = "StrongPass1"


class _FakePage:
    def __init__(self):
        self.overlay = []
        self.width = 1280
        self.height = 800
        self.on_keyboard_event = None
        self.opened = []
        self.updated = 0

    def update(self, *a, **k):
        self.updated += 1

    def open(self, control):
        self.opened.append(control)
        try:
            control.open = True
        except Exception:
            pass

    @staticmethod
    def close(control):
        try:
            control.open = False
        except Exception:
            pass

    def run_task(self, handler, *args, **kwargs):
        try:
            result = handler(*args, **kwargs)
        except Exception:
            return
        close = getattr(result, "close", None)
        if close is not None:
            close()


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-restore-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    item = ctx.items.create(name="مادة اختبار", selling_price=2.0, purchase_price=1.0, quantity=10)
    ctx.invoices.create_invoice(
        invoice_type="sale", paid_amount=0,
        lines=[InvoiceLineInput(description="x", item_id=int(item), quantity=2, unit_price=2.0)],
    )
    return ctx, tmp


def test_backup_and_rebuild_emit_progress():
    ctx, tmp = _ctx()
    try:
        backup_dir = Path(tmp) / "backups"
        backup_dir.mkdir()
        stages: list[str] = []
        ctx.backup.create_backup(backup_dir / "b.nanobackup", progress=lambda s, *a: stages.append(s))
        assert "safety_backup" in stages and "backup_zip" in stages

        verify: list[str] = []
        ctx.backup.validate_backup(backup_dir / "b.nanobackup", progress=lambda s, *a: verify.append(s))
        assert "extract" in verify and "hash" in verify and "integrity" in verify

        rebuild: list[str] = []
        ctx.invoices.rebuild_derived_state(progress=lambda s, *a: rebuild.append(s))
        assert "rebuild_inventory" in rebuild and "rebuild_ledger_invoices" in rebuild
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_event_aborts_validation():
    ctx, tmp = _ctx()
    try:
        backup_dir = Path(tmp) / "backups"
        backup_dir.mkdir()
        path = ctx.backup.create_backup(backup_dir / "b.nanobackup")
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(OperationCancelled):
            ctx.backup.validate_backup(path, progress=lambda *a: None, cancel=cancel)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_restore_flow_guards_against_second_window():
    ctx, tmp = _ctx()
    try:
        backup_dir = Path(tmp) / "backups"
        backup_dir.mkdir()
        path = ctx.backup.create_backup(backup_dir / "b.nanobackup")
        page = _FakePage()
        notices: list[str] = []
        flow = RestoreFlow(page, ctx, None, notify=lambda t, **k: notices.append(t), on_logout=lambda: None)

        assert flow.open(path) is True
        assert flow.active is True
        assert page.opened and isinstance(page.opened[-1], ft.AlertDialog)
        # The guard refuses a second concurrent window.
        assert flow.open(path) is False
        assert notices and "قيد التنفيذ" in notices[-1]

        # Live rendering is safe and reflects the mapped ratio.
        flow._handle_progress("hash", 50, 100, "bytes", "التحقق من البصمة")
        flow._render()
        assert flow._controls["percent"].value.endswith("%")
        assert flow._ratio_for("swap", 1, 1) > flow._ratio_for("hash", 0, 100)

        flow._close()
        assert flow.active is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
