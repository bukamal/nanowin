"""Modern, smart "back up before you leave" window (desktop).

Flet delivers the window-close request as a ``WindowEventType.CLOSE`` event
once ``page.window.prevent_close`` is set, so the app can run one last task
instead of just vanishing. This module wires that event to a wide, modern
window in the same visual language as the restore flow:

* **smart** -- reads how stale the newest local backup is (age + size +
  count), adapts the wording/emphasis, and shows a health chip.
* **real progress** -- drives ``BackupService.create_backup(progress=...)``
  with a percentage, stage steps, live detail and speed; the operation can be
  cancelled without closing the app.
* **safe close** -- after success it shows a short countdown (cancellable)
  before exiting; on failure it stays open with retry / exit-without options.
* **quiet option** -- a "تذكّر" switch turns on automatic backup-on-exit; the
  automatic path now shows the same progress window instead of closing
  silently.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime

import flet as ft

from nano_offline.core import backup_settings
from nano_offline.core import branding
from nano_offline.core.cancellation import OperationCancelled
from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.components.task_progress import BACKUP_STEPS, ProgressSection

_STAGE_STEP = {
    "safety_backup": "snapshot",
    "integrity": "pack",
    "backup_hash": "verify",
    "backup_zip": "pack",
    "cleanup": "prune",
}


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
    """Intercept the window close and offer a modern backup window."""
    try:
        page.window.prevent_close = True
    except Exception:
        return  # platform without an interceptable window: leave it alone

    state: dict = {
        "open": False, "closing": False, "busy": False, "done": False,
        "auto": False, "cancel": None, "error": None, "ratio": 0.0,
        "stage": "snapshot", "detail": "", "metric": "", "countdown": None,
    }
    controls: dict = {}

    def _close_for_real() -> None:
        state["closing"] = True
        state["countdown"] = None
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

    # ------------------------------------------------------------------ build
    def _build() -> None:
        info = _latest_backup(ctx)
        hours = _stale_hours(info)
        if info["count"] == 0 and hours is None:
            tone, tone_bg = Colors.WARNING_DARKER, Colors.WARNING_BG
            health = "لا توجد نسخة احتياطية بعد"
            hint = "إنشاء نسخة الآن يحمي بياناتك قبل الخروج."
        elif hours is not None and hours >= 24:
            tone, tone_bg = Colors.WARNING_DARKER, Colors.WARNING_BG
            health = f"آخر نسخة {_age_text(hours * 3600)}"
            hint = "مرّ وقت طويل — يُنصح بإنشاء نسخة حديثة قبل الخروج."
        else:
            tone, tone_bg = Colors.SUCCESS_DARK, Colors.SUCCESS_BG
            health = f"نسختك حديثة — {_age_text((hours or 0) * 3600)}"
            hint = "يمكنك الخروج مباشرة، أو إنشاء نسخة إضافية الآن."

        controls["health"] = ft.Text(health, size=12, weight=ft.FontWeight.W_700, color=tone)
        controls["hint"] = ft.Text(hint, size=11, color=Colors.TEXT_MUTED)

        def info_row(icon, label: str, value: str) -> ft.Control:
            return ft.Row(
                [
                    ft.Container(ft.Icon(icon, size=15, color=Colors.TEXT_SECONDARY),
                                 width=30, height=30, alignment=ft.alignment.center,
                                 bgcolor=Colors.BACKGROUND, border_radius=Radius.SM),
                    ft.Column([ft.Text(label, size=10, color=Colors.TEXT_SECONDARY),
                               ft.Text(value, size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_PRIMARY)],
                              spacing=1, expand=True, tight=True),
                ],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

        retention = backup_settings.retention_count(ctx.settings)
        retention_text = "بلا حد (يُحتفظ بكل النسخ)" if retention <= 0 else f"يُحتفظ بآخر {retention} نسخة"
        controls["info_left"] = ft.Container(
            ft.Column([
                info_row(ft.Icons.HISTORY, "آخر نسخة", _age_text((hours or 0) * 3600) if info["ts"] else "لا يوجد سجل"),
                info_row(ft.Icons.INVENTORY_2_OUTLINED, "عدد النسخ", f"{info['count']} نسخة · {_human_size(info['total'])}"),
                info_row(ft.Icons.RULE, "سياسة الاحتفاظ", retention_text),
            ], spacing=10),
            padding=14, bgcolor=Colors.WHITE, border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.MD,
        )
        controls["remember"] = ft.Switch(
            label="أنشئ نسخة تلقائيًا في المرات القادمة", value=False,
            active_color=Colors.PRIMARY, label_position=ft.LabelPosition.LEFT,
        )
        controls["info_right"] = ft.Container(
            ft.Column([
                ft.Row([ft.Icon(ft.Icons.LOCK_OUTLINE, size=15, color=Colors.TEXT_FAINT),
                        ft.Text("نسخة محلية آمنة — لا تُرسل أي بيانات عبر الشبكة.",
                                size=11, color=Colors.TEXT_SECONDARY, expand=True)], spacing=8),
                ft.Divider(height=1, color=Colors.BORDER_ALT),
                controls["remember"],
                ft.Text(
                    f"المسار: {ctx.backup.default_dir()}", size=10, color=Colors.TEXT_FAINT,
                    selectable=True, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                ),
            ], spacing=10),
            padding=14, bgcolor=Colors.BACKGROUND_ALT, border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.MD,
        )
        # Summary pane (start side): health + hint + backup facts. Laid out
        # vertically so the window reads as two side-by-side columns.
        controls["info_section"] = ft.Column(
            [controls["info_left"], controls["info_right"]], spacing=10, visible=True,
        )

        # progress ---------------------------------------------------------
        progress = ProgressSection(orientation="horizontal", steps=BACKUP_STEPS)
        controls["progress"] = progress
        controls.update(progress.controls)
        controls["progress_section"] = ft.Container(progress.control, visible=False)

        # success / error --------------------------------------------------
        controls["success_name"] = ft.Text("", size=12, weight=ft.FontWeight.W_600, color=Colors.SUCCESS_DARK)
        controls["countdown"] = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
        controls["cancel_exit_btn"] = ft.TextButton("إلغاء الخروج", on_click=lambda _=None: _stay_open())
        controls["success_section"] = ft.Column([
            ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, size=40, color=Colors.SUCCESS),
                    ft.Column([ft.Text("تم إنشاء النسخة الاحتياطية", size=15, weight=ft.FontWeight.BOLD, color=Colors.SUCCESS_DARK),
                               controls["success_name"], controls["countdown"]], spacing=2, expand=True, tight=True)],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, visible=False)

        controls["error_text"] = ft.Text("", size=11.5, color=Colors.DANGER_DARK, selectable=True)
        controls["error_section"] = ft.Column([
            ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE_ROUNDED, size=34, color=Colors.DANGER),
                    ft.Column([ft.Text("تعذّر إنشاء النسخة", size=14, weight=ft.FontWeight.BOLD, color=Colors.DANGER_DARK),
                               controls["error_text"]], spacing=2, expand=True, tight=True)],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, visible=False)

        # header -----------------------------------------------------------
        header = ft.Container(
            ft.Row([
                ft.Container(ft.Icon(ft.Icons.SHIELD_OUTLINED, color=Colors.WHITE, size=22),
                             width=44, height=44, alignment=ft.alignment.center,
                             bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE), border_radius=14),
                ft.Column([ft.Text("قبل أن تغادر", size=18, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                           ft.Text(f"احتفظ بنسخة من بيانات {branding.APP_NAME_AR} على هذا الجهاز",
                                   size=11, color=ft.Colors.with_opacity(0.9, ft.Colors.WHITE))],
                          spacing=1, expand=True, tight=True),
            ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.symmetric(horizontal=16, vertical=14),
            gradient=ft.LinearGradient(colors=[Colors.BRAND_START, Colors.BRAND_CYAN],
                                       begin=ft.alignment.top_left, end=ft.alignment.bottom_right),
            border_radius=ft.border_radius.only(top_left=18, top_right=18),
        )

        controls["status_chip"] = ft.Container(
            ft.Row([ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=15, color=tone), controls["health"]], spacing=8),
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            bgcolor=tone_bg, border_radius=Radius.MD,
        )

        # idle call-to-action (fills the work pane before the backup starts)
        controls["idle_panel"] = ft.Container(
            ft.Column([
                ft.Container(ft.Icon(ft.Icons.CLOUD_DONE_OUTLINED, size=32, color=Colors.PRIMARY),
                             width=64, height=64, alignment=ft.alignment.center,
                             bgcolor=Colors.PRIMARY_BG, border_radius=20),
                ft.Text("جاهز للنسخ الاحتياطي", size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                ft.Text("اضغط «إنشاء نسخة والخروج» لحفظ بياناتك بأمان، أو اخرج مباشرة.",
                        size=11, color=Colors.TEXT_SECONDARY, text_align=ft.TextAlign.CENTER),
            ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=18, bgcolor=Colors.BACKGROUND_ALT, border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.MD, alignment=ft.alignment.center, expand=True, visible=True,
        )

        # actions ----------------------------------------------------------
        controls["cancel_btn"] = ft.TextButton("إلغاء", on_click=_on_cancel)
        controls["exit_btn"] = ft.OutlinedButton("خروج بدون نسخة", icon=ft.Icons.LOGOUT, on_click=lambda _=None: _exit_now())
        controls["create_btn"] = ft.FilledButton("إنشاء نسخة والخروج", icon=ft.Icons.SHIELD_OUTLINED, on_click=_on_create)
        controls["retry_btn"] = ft.FilledButton("إعادة المحاولة", icon=ft.Icons.REFRESH, on_click=_on_create, visible=False)
        # All actions live in one bottom bar (wrap so they never overflow the
        # width on a phone).
        controls["actions"] = ft.Row(
            [controls["cancel_exit_btn"], controls["cancel_btn"], controls["exit_btn"], controls["retry_btn"], controls["create_btn"]],
            spacing=8, alignment=ft.MainAxisAlignment.END, wrap=True, run_spacing=8,
        )

        try:
            available = int(getattr(page, "width", 0) or 0)
        except Exception:
            available = 0
        dialog_width = 820 if available <= 0 else max(360, min(820, available - 48))

        # Two horizontal panes: the facts/health on one side, the live work
        # (progress / success / error) on the other. Falls back to a stacked
        # column only on very narrow screens.
        controls["aside"] = ft.Column(
            [controls["status_chip"], controls["hint"], controls["info_section"]],
            spacing=10, expand=True,
        )
        controls["work"] = ft.Column(
            [controls["idle_panel"], controls["progress_section"],
             controls["success_section"], controls["error_section"]],
            spacing=10, expand=True,
        )
        controls["body_cols"] = (
            ft.Row([controls["aside"], controls["work"]], spacing=16,
                   vertical_alignment=ft.CrossAxisAlignment.START, expand=True)
            if dialog_width >= 660
            else ft.Column([controls["aside"], controls["work"]], spacing=12)
        )
        body = ft.Container(controls["body_cols"], width=dialog_width,
                            padding=ft.padding.only(left=16, right=16, top=14, bottom=6))
        controls["footer"] = ft.Container(
            controls["actions"],
            padding=ft.padding.only(left=16, right=16, top=10, bottom=14),
            border=ft.border.only(top=ft.BorderSide(1, Colors.BORDER_ALT)),
        )
        root = ft.Container(
            ft.Column([header, body, controls["footer"]], spacing=0),
            width=dialog_width, bgcolor=Colors.WHITE, border_radius=18, shadow=Shadow.LG, rtl=True)
        controls["root"] = root
        dialog = ft.AlertDialog(modal=True, content=root, content_padding=0)
        controls["dialog"] = dialog
        _render(fresh=True)

    # ---------------------------------------------------------------- render
    def _render(*, fresh: bool = False) -> None:
        if "dialog" not in controls:
            return
        busy = state["busy"]
        done = state["done"]
        err = state["error"] is not None
        auto = state["auto"]
        idle = not busy and not done and not err
        controls["info_section"].visible = idle and not auto
        if controls.get("idle_panel") is not None:
            controls["idle_panel"].visible = idle and not auto
        controls["progress_section"].visible = (busy or (auto and not done and not err))
        controls["success_section"].visible = done and not err
        controls["error_section"].visible = err and not busy
        controls["create_btn"].visible = idle and not auto
        controls["exit_btn"].visible = idle and not auto
        controls["retry_btn"].visible = err and not busy
        controls["cancel_btn"].visible = not done
        controls["cancel_btn"].disabled = False
        controls["cancel_exit_btn"].visible = done and not err and state.get("countdown") is not None
        try:
            controls["dialog"].update()
        except Exception:
            pass

    def _render_progress() -> None:
        progress = controls.get("progress")
        if progress is None:
            return
        ratio = max(0.0, min(1.0, float(state.get("ratio") or 0.0)))
        stage_key = str(state.get("stage") or "snapshot")
        if stage_key not in {key for key, _l, _i in BACKUP_STEPS}:
            stage_key = {"safety_backup": "snapshot", "integrity": "verify",
                         "backup_hash": "verify", "backup_zip": "pack",
                         "cleanup": "prune"}.get(stage_key, "snapshot")
        if state.get("done") and not state.get("error"):
            states = {key: "done" for key, _l, _i in BACKUP_STEPS}
        elif state.get("error"):
            keys = [k for k, _l, _i in BACKUP_STEPS]
            idx = keys.index(stage_key) if stage_key in keys else 0
            states = {
                k: ("done" if i < idx else "error" if i == idx else "pending")
                for i, (k, _l, _i) in enumerate(BACKUP_STEPS)
            }
        else:
            states = None
        title = next((label for key, label, _i in BACKUP_STEPS if key == stage_key), "جارٍ العمل...")
        progress.render(
            ratio=ratio,
            indeterminate=bool(state.get("indeterminate")) and not state.get("done"),
            stage_key=stage_key,
            title=title,
            detail=str(state.get("detail") or ""),
            metric=str(state.get("metric") or ""),
            states=states,
        )

    async def _pump() -> None:
        while state["busy"] and state["open"]:
            _render_progress()
            await asyncio.sleep(0.1)
        _render_progress()

    # --------------------------------------------------------------- actions
    def _cancel_dialog(_=None) -> None:
        state["open"] = False
        try:
            page.close(controls["dialog"])
        except Exception:
            pass

    def _exit_now(_=None) -> None:
        _cancel_dialog()
        _close_for_real()

    def _stay_open() -> None:
        # Cancel the success countdown: keep the app open on the prompt.
        state["countdown"] = None
        state["done"] = False
        state["busy"] = False
        state["error"] = None
        if "info_section" in controls:
            controls["info_section"].visible = True
        _render()

    def _on_cancel(_=None) -> None:
        # While a backup is running, cancel the operation but stay in the app.
        if state["busy"] and state.get("cancel") is not None:
            try:
                state["cancel"].set()
                controls["cancel_btn"].disabled = True
                controls["detail"].value = "جارٍ الإلغاء..."
                controls["root"].update()
            except Exception:
                pass
            return
        _cancel_dialog()

    def _on_create(_=None) -> None:
        if state["busy"]:
            return
        page.run_task(_run_backup)

    def _progress_cb(stage, done, total, unit, detail=""):
        try:
            state["stage"] = _STAGE_STEP.get(str(stage), state.get("stage", "snapshot"))
            if total and float(total) > 0:
                state["ratio"] = max(0.0, min(1.0, float(done) / float(total)))
            detail = detail or ""
            bits = []
            now = time.monotonic()
            prev_done = float(state.get("_last_done") or 0)
            prev_t = float(state.get("_last_t") or now)
            dt = max(now - prev_t, 1e-6)
            if float(done) >= prev_done:
                speed = (float(done) - prev_done) / dt
                if str(unit) == "bytes" and speed > 0:
                    bits.append(f"{_human_size(speed)}/ث")
                    if total and float(total) > float(done):
                        bits.append(f"متبقٍ ~{int((float(total) - float(done)) / speed)}s")
                elif speed > 0 and str(unit) not in ("", "bytes"):
                    bits.append(f"{int(speed):,}/ث")
            state["_last_done"] = float(done)
            state["_last_t"] = now
            state["detail"] = detail
            state["metric"] = " · ".join(bits)
        except Exception:
            pass

    async def _run_backup() -> None:
        state.update({"busy": True, "done": False, "error": None, "ratio": 0.0,
                      "stage": "snapshot", "detail": "جارٍ تجهيز النسخة الاحتياطية...", "metric": ""})
        cancel = threading.Event()
        state["cancel"] = cancel
        _render()
        page.run_task(_pump)
        directory = ctx.backup.default_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = directory / f"nano_backup_{stamp}.nanobackup"
            await asyncio.to_thread(
                ctx.backup.create_backup, target, progress=_progress_cb, cancel=cancel
            )
            state["stage"] = "cleanup"
            state["ratio"] = 1.0
            keep = backup_settings.retention_count(ctx.settings)
            if keep > 0:
                await asyncio.to_thread(ctx.backup.prune_backups, directory, keep)
            if controls.get("remember") is not None and controls["remember"].value:
                try:
                    ctx.settings.set(backup_settings.EXIT_AUTO_KEY, "1")
                except Exception:
                    pass
            try:
                ctx.notifications.record_backup_completed()
            except Exception:
                pass
            state.update({"busy": False, "done": True})
            controls["success_name"].value = f"{target.name} · {_human_size(target.stat().st_size)}"
            _render()
            _render_progress()
            await _start_countdown()
        except OperationCancelled:
            state.update({"busy": False, "done": False, "error": None,
                          "detail": "تم إلغاء إنشاء النسخة — لم يتم الخروج."})
            if "hint" in controls:
                controls["hint"].value = "تم إلغاء إنشاء النسخة — يمكنك الخروج بدون نسخة أو المحاولة مجددًا."
            _render()
        except Exception as exc:
            state.update({"busy": False, "done": False, "error": str(exc), "stage": "snapshot"})
            if "error_text" in controls:
                controls["error_text"].value = str(exc)
            _render()

    async def _start_countdown() -> None:
        state["countdown"] = 3
        while state.get("countdown") and state["countdown"] > 0:
            if "countdown" in controls:
                controls["countdown"].value = f"سيتم الخروج تلقائيًا خلال {state['countdown']} ثانية — اضغط «إلغاء الخروج» للبقاء."
            _render()
            await asyncio.sleep(1)
            if state.get("countdown") is None:
                return
            state["countdown"] -= 1
        if state.get("countdown") is not None:
            _close_for_real()

    async def _auto_backup_and_close() -> None:
        state["auto"] = True
        _render()
        await _run_backup()

    # ------------------------------------------------------------ window hook
    def _on_window_event(e) -> None:
        if getattr(e, "type", None) != ft.WindowEventType.CLOSE:
            return
        if state["closing"] or state["busy"]:
            return
        try:
            if not backup_settings.exit_ask(ctx.settings):
                _close_for_real()
                return
            auto = backup_settings.exit_auto(ctx.settings)
        except Exception:
            _close_for_real()
            return
        if state["open"]:
            return
        state["open"] = True
        if "dialog" not in controls:
            _build()
        if auto:
            state["auto"] = True
            try:
                page.open(controls["dialog"])
            except Exception:
                state["open"] = False
                _close_for_real()
                return
            page.run_task(_auto_backup_and_close)
        else:
            state["auto"] = False
            try:
                page.open(controls["dialog"])
            except Exception:
                state["open"] = False
                _close_for_real()
                return

    page.window.on_event = _on_window_event


__all__ = ["install_exit_backup"]
