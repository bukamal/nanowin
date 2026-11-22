from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from nano_offline.app_context import AppContext
from nano_offline.core import backup_settings
from nano_offline.components.exit_backup import install_exit_backup


class _Window:
    def __init__(self):
        self.prevent_close = False
        self.on_event = None
        self.closed = False

    def close(self):
        self.closed = True

    def destroy(self):
        self.closed = True


class _Page:
    def __init__(self):
        self.window = _Window()
        self.overlay = []
        self.opened = []
        self.width = 1280
        self.updated = 0

    def update(self, *a, **k):
        self.updated += 1

    def open(self, control):
        self.opened.append(control)

    @staticmethod
    def close(control):
        pass

    def run_task(self, handler, *args, **kwargs):
        try:
            result = handler(*args, **kwargs)
        except Exception:
            return
        close = getattr(result, "close", None)
        if close is not None:
            close()


def _texts(control):
    out = []
    if isinstance(control, ft.Text):
        out.append(str(control.value))
    for child in getattr(control, "controls", None) or ():
        out.extend(_texts(child))
    inner = getattr(control, "content", None)
    if inner is not None and not isinstance(inner, (str, bytes)):
        out.extend(_texts(inner))
    return out


def _ctx():
    tmp = tempfile.mkdtemp(prefix="nano-exit-")
    return AppContext.create(Path(tmp) / "nano.db"), tmp


def test_exit_prompt_intercepts_close_and_builds_modern_window():
    ctx, tmp = _ctx()
    try:
        page = _Page()
        install_exit_backup(page, ctx)
        assert page.window.prevent_close is True
        assert callable(page.window.on_event)

        page.window.on_event(SimpleNamespace(type=ft.WindowEventType.CLOSE))
        assert page.opened and isinstance(page.opened[-1], ft.AlertDialog)
        labels = _texts(page.opened[-1])
        assert "قبل أن تغادر" in labels
        assert any("إنشاء النسخة" in label for label in labels)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_exit_auto_mode_opens_the_progress_window():
    ctx, tmp = _ctx()
    try:
        ctx.settings.set(backup_settings.EXIT_ASK_KEY, "1")
        ctx.settings.set(backup_settings.EXIT_AUTO_KEY, "1")
        page = _Page()
        install_exit_backup(page, ctx)
        page.window.on_event(SimpleNamespace(type=ft.WindowEventType.CLOSE))
        assert page.opened and isinstance(page.opened[-1], ft.AlertDialog)
        assert "قبل أن تغادر" in _texts(page.opened[-1])
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
