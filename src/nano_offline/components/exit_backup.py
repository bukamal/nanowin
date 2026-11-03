"""Modern, smart "back up before you leave" prompt (desktop).

Flet delivers the window-close request as a ``WindowEventType.CLOSE`` event
once ``page.window.prevent_close`` is set, so the app can run one last task
instead of just vanishing. This module wires that event to a modern dialog:

* **smart** -- it reads how stale the newest local backup is (age + size +
  count) and adapts the wording and emphasis, so a shop that just backed up
  sees "نسختك حديثة" while one that never did gets a clear warning.
* **quiet option** -- a "تذكّر" switch turns on automatic backup-on-exit so
  future closes skip the dialog entirely and just snapshot first.
* **safe** -- the backup runs off the UI thread with a live progress state,
  reuses the same verified/encrypted-capable ``BackupService.create_backup``,
  prunes per the retention setting, and resets the backup reminder rule.

Everything is best-effort: if the window can't be intercepted (or anything
throws) the app still closes normally.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import flet as ft

from nano_offline.core import backup_settings
from nano_offline.core import branding
from nano_offline.core.theme import Colors, Radius


def _human_size(num: float) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _age_text(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    if seconds < 90:
        return "الآن"
    minutes = seconds / 60
    if minutes < 90:
        return f"منذ {int(minutes)} دقيقة"
    hours = minutes / 60
    if hours < 36:
        return f"منذ {int(hours)} ساعة"
    return f"منذ {int(hours / 24)} يوم"


def _latest_backup(ctx) -> dict:
    """Newest local backup file + totals, or the recorded timestamp."""
    info = {"ts": None, "name": "", "count": 0, "total": 0.0}
    try:
        directory = ctx.backup.default_dir()
        files = [
            p for p in list(directory.glob("*.nanobackup")) + list(directory.glob("*.qeidbackup"))
            if p.is_file()
        ]
    except Exception:
        files = []
    if files:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        try:
            info["total"] = sum(p.stat().st_size for p in files)
        except Exception:
            info["total"] = 0.0
        info["count"] = len(files)
        info["name"] = files[0].name
        try:
            info["ts"] = files[0].stat().st_mtime
        except Exception:
            info["ts"] = None
    else:
        try:
            raw = ctx.notifications.settings.get("last_backup_at", "")
            if raw:
                info["ts"] = datetime.fromisoformat(str(raw)).timestamp()
        except Exception:
            pass
    return info


def install_exit_backup(page: ft.Page, ctx, *, native_files=None) -> None:
    """Intercept the window close and offer a modern backup prompt."""
    try:
        page.window.prevent_close = True
    except Exception:
        return  # platform without an interceptable window: leave it alone

    state = {"open": False, "closing": False, "busy": False}

    def _close_for_real() -> None:
        state["closing"] = True
        try:
            page.window.prevent_close = False
        except Exception:
            pass
        try:
            page.window.close()
        except Exception:
            try:
                page.window.destroy()
            except Exception:
                pass

    def _stale_hours(info: dict) -> float | None:
        if not info.get("ts"):
            return None
        return max(0.0, (datetime.now().timestamp() - float(info["ts"])) / 3600.0)

    def _build_dialog() -> ft.AlertDialog:
        info = _latest_backup(ctx)
        hours = _stale_hours(info)
        if info["count"] == 0 and hours is None:
            tone, tone_bg, headline = Colors.WARNING_DARKER, Colors.WARNING_BG, "لا توجد نسخة احتياطية بعد"
            hint = "إنشاء نسخة الآن يحمي بياناتك قبل الخروج."
        elif hours is not None and hours >= 24:
            tone, tone_bg = Colors.WARNING_DARKER, Colors.WARNING_BG
            headline = f"آخر نسخة {_age_text(hours * 3600)}"
            hint = "مرّ وقت طويل — يُنصح بإنشاء نسخة حديثة قبل الخروج."
        else:
            tone, tone_bg = Colors.SUCCESS_DARK, Colors.SUCCESS_BG
            headline = f"نسختك حديثة — آخر نسخة {_age_text((hours or 0) * 3600)}"
            hint = "يمكنك الخروج مباشرة، أو إنشاء نسخة إضافية الآن."

        stat = ft.Text(f"{headline} · {info['count']} نسخة · {_human_size(info['total'])}",
                       size=12, weight=ft.FontWeight.W_600, color=tone)
        status = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
        progress = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)
        remember = ft.Switch(label="أنشئ نسخة تلقائيًا في المرات القادمة", value=False, label_position=ft.LabelPosition.LEFT)

        create_btn = ft.FilledButton("إنشاء نسخة والخروج", icon=ft.Icons.SHIELD_OUTLINED)
        exit_btn = ft.OutlinedButton("خروج بدون نسخة", icon=ft.Icons.LOGOUT)
        cancel_btn = ft.TextButton("إلغاء")

        dialog = ft.AlertDialog(modal=True)

        def _set_busy(busy: bool, text: str = "") -> None:
            state["busy"] = busy
            progress.visible = busy
            create_btn.disabled = busy
            exit_btn.disabled = busy
            cancel_btn.disabled = busy
            status.value = text
            try:
                dialog.update()
            except Exception:
                pass

        def _cancel(_=None) -> None:
            state["open"] = False
            try:
                page.close(dialog)
            except Exception:
                pass

        def _exit_now(_=None) -> None:
            _cancel()
            _close_for_real()

        async def _run_backup() -> None:
            _set_busy(True, "جارٍ تجهيز النسخة الاحتياطية...")
            try:
                directory = ctx.backup.default_dir()
                directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                target = directory / f"nano_backup_{stamp}.nanobackup"
                await asyncio.to_thread(ctx.backup.create_backup, target)
                if remember.value:
                    try:
                        ctx.settings.set(backup_settings.EXIT_AUTO_KEY, "1")
                    except Exception:
                        pass
                keep = backup_settings.retention_count(ctx.settings)
                if keep > 0:
                    await asyncio.to_thread(ctx.backup.prune_backups, directory, keep)
                try:
                    ctx.notifications.record_backup_completed()
                except Exception:
                    pass
                _set_busy(True, f"تم إنشاء {target.name} ✓ — يتم الآن الخروج...")
                await asyncio.sleep(0.5)
            except Exception as exc:
                _set_busy(False, f"تعذّر إنشاء النسخة: {exc}")
                return
            _close_for_real()

        def _on_create(_=None) -> None:
            page.run_task(_run_backup)

        create_btn.on_click = _on_create
        exit_btn.on_click = _exit_now
        cancel_btn.on_click = _cancel

        dialog.title = ft.Row(
            [
                ft.Container(
                    ft.Icon(ft.Icons.CLOUD_DONE_OUTLINED, color=Colors.WHITE, size=22),
                    width=44, height=44, alignment=ft.alignment.center,
                    gradient=ft.LinearGradient(
                        colors=[Colors.BRAND_START, Colors.BRAND_DEEP],
                        begin=ft.alignment.top_left, end=ft.alignment.bottom_right,
                    ),
                    border_radius=14,
                ),
                ft.Column(
                    [
                        ft.Text("قبل أن تغادر", size=18, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                        ft.Text(f"احتفظ بنسخة من بيانات {branding.APP_NAME_AR} على هذا الجهاز", size=11, color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=1, tight=True,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        dialog.content = ft.Container(
            ft.Column(
                [
                    ft.Container(
                        ft.Row([ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=16, color=tone), stat], spacing=8),
                        padding=ft.padding.symmetric(horizontal=12, vertical=10),
                        bgcolor=tone_bg, border_radius=Radius.MD,
                    ),
                    ft.Row(
                        [ft.Icon(ft.Icons.LOCK_OUTLINE, size=15, color=Colors.TEXT_FAINT),
                         ft.Text("نسخة محلية آمنة — لا تُرسل أي بيانات عبر الشبكة.", size=11, color=Colors.TEXT_SECONDARY, expand=True)],
                        spacing=8,
                    ),
                    ft.Text(hint, size=11, color=Colors.TEXT_MUTED),
                    remember,
                    ft.Row([progress, status], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ],
                spacing=12, tight=True,
            ),
            width=440,
        )
        dialog.actions = [cancel_btn, exit_btn, create_btn]
        dialog.actions_alignment = ft.MainAxisAlignment.END
        return dialog

    async def _auto_backup_and_close() -> None:
        directory = ctx.backup.default_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await asyncio.to_thread(ctx.backup.create_backup, directory / f"nano_backup_{stamp}.nanobackup")
            keep = backup_settings.retention_count(ctx.settings)
            if keep > 0:
                await asyncio.to_thread(ctx.backup.prune_backups, directory, keep)
            try:
                ctx.notifications.record_backup_completed()
            except Exception:
                pass
        except Exception:
            pass
        _close_for_real()

    def _on_window_event(e) -> None:
        if getattr(e, "type", None) != ft.WindowEventType.CLOSE:
            return
        if state["closing"] or state["busy"]:
            return
        try:
            if not backup_settings.exit_ask(ctx.settings):
                _close_for_real()
                return
            if backup_settings.exit_auto(ctx.settings):
                state["closing"] = True
                page.run_task(_auto_backup_and_close)
                return
        except Exception:
            _close_for_real()
            return
        if state["open"]:
            return
        state["open"] = True
        try:
            page.open(_build_dialog())
        except Exception:
            state["open"] = False
            _close_for_real()

    page.window.on_event = _on_window_event


__all__ = ["install_exit_backup"]
