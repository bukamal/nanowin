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

_STEPS = [
    ("snapshot", "لقطة القاعدة", ft.Icons.SAVE_OUTLINED),
    ("pack", "التحقق والضغط", ft.Icons.FOLDER_ZIP),
    ("prune", "تنظيف النسخ", ft.Icons.CLEANING_SERVICES),
    ("exit", "الخروج", ft.Icons.LOGOUT),
]
_STAGE_STEP = {
    "safety_backup": "snapshot",
    "integrity": "pack",
    "backup_hash": "pack",
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
            border_radius=Radius.MD, expand=True,
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
                ft.Text(f"المجلد: {ctx.backup.default_dir().name}", size=10, color=Colors.TEXT_FAINT),
            ], spacing=10),
            padding=14, bgcolor=Colors.BACKGROUND_ALT, border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.MD, expand=True,
        )
        controls["info_section"] = ft.Row(
            [controls["info_left"], controls["info_right"]],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.START, visible=True,
        )

        # progress ---------------------------------------------------------
        controls["percent"] = ft.Text("0%", size=24, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY)
        controls["bar"] = ft.ProgressBar(value=0.0, height=8, color=Colors.PRIMARY,
                                         bgcolor=Colors.PRIMARY_BG, border_radius=8)
        controls["stage_title"] = ft.Text("جارٍ التحضير...", size=13, weight=ft.FontWeight.W_700, color=Colors.TEXT_PRIMARY)
        controls["detail"] = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
        controls["metric"] = ft.Text("", size=10.5, color=Colors.TEXT_FAINT)
        step_boxes = {}
        step_row = ft.Row(spacing=8, wrap=True)
        for key, label, icon in _STEPS:
            icon_ctl = ft.Icon(icon, size=15, color=Colors.TEXT_FAINT)
            text_ctl = ft.Text(label, size=10.5, color=Colors.TEXT_MUTED)
            box = ft.Container(
                ft.Row([icon_ctl, text_ctl], spacing=6, tight=True),
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                border=ft.border.all(1, Colors.BORDER), border_radius=12,
                bgcolor=Colors.WHITE,
            )
            step_boxes[key] = (box, icon_ctl, text_ctl)
            step_row.controls.append(box)
        controls["steps"] = step_boxes
        controls["step_row"] = step_row
        controls["progress_section"] = ft.Column([
            ft.Row([controls["percent"], ft.Container(expand=True), controls["metric"]]),
            controls["bar"], controls["stage_title"], controls["detail"], step_row,
        ], spacing=10, visible=False)

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

        # actions ----------------------------------------------------------
        controls["cancel_btn"] = ft.TextButton("إلغاء", on_click=_on_cancel)
        controls["exit_btn"] = ft.OutlinedButton("خروج بدون نسخة", icon=ft.Icons.LOGOUT, on_click=lambda _=None: _exit_now())
        controls["create_btn"] = ft.FilledButton("إنشاء نسخة والخروج", icon=ft.Icons.SHIELD_OUTLINED, on_click=_on_create)
        controls["retry_btn"] = ft.FilledButton("إعادة المحاولة", icon=ft.Icons.REFRESH, on_click=_on_create, visible=False)
        controls["actions"] = ft.Row(
            [controls["cancel_exit_btn"], controls["cancel_btn"], controls["exit_btn"], controls["retry_btn"], controls["create_btn"]],
            spacing=8, alignment=ft.MainAxisAlignment.END,
        )

        try:
            available = int(getattr(page, "width", 0) or 0)
        except Exception:
            available = 0
        dialog_width = 760 if available <= 0 else max(340, min(760, available - 48))
        body = ft.Container(
            ft.Column([
                ft.Container(ft.Column([controls["status_chip"], controls["hint"]], spacing=8),
                             padding=ft.padding.only(left=16, right=16, top=14, bottom=2)),
                ft.Container(controls["info_section"], padding=ft.padding.only(left=16, right=16, top=10, bottom=6)),
                ft.Container(controls["progress_section"], padding=ft.padding.symmetric(horizontal=16)),
                ft.Container(controls["success_section"], padding=ft.padding.symmetric(horizontal=16)),
                ft.Container(controls["error_section"], padding=ft.padding.symmetric(horizontal=16)),
            ], spacing=4), width=dialog_width,
        )
        root = ft.Container(ft.Column([header, body, ft.Container(controls["actions"], padding=ft.padding.only(left=16, right=16, top=6, bottom=14))],
                                     spacing=0),
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
        if "percent" not in controls:
            return
        ratio = max(0.0, min(1.0, float(state.get("ratio") or 0.0)))
        controls["percent"].value = f"{int(round(ratio * 100))}%"
        controls["bar"].value = ratio
        step_key = str(state.get("stage") or "snapshot")
        title = next((label for key, label, _i in _STEPS if key == step_key), "جارٍ العمل...")
        controls["stage_title"].value = title
        controls["detail"].value = str(state.get("detail") or "")
        controls["metric"].value = str(state.get("metric") or "")
        order = [k for k, _l, _i in _STEPS]
        idx = order.index(step_key) if step_key in order else 0
        for i, key in enumerate(order):
            box, icon_ctl, text_ctl = controls["steps"][key]
            if state.get("done") and not state.get("error"):
                st = "done"
            elif i < idx:
                st = "done"
            elif i == idx:
                st = "error" if state.get("error") else "current"
            else:
                st = "pending"
            if st == "done":
                icon_ctl.name, icon_ctl.color = ft.Icons.CHECK_CIRCLE_ROUNDED, Colors.SUCCESS
                text_ctl.color = Colors.SUCCESS_DARK
            elif st == "current":
                icon_ctl.name, icon_ctl.color = ft.Icons.SYNC_ROUNDED, Colors.PRIMARY
                text_ctl.color = Colors.PRIMARY
            elif st == "error":
                icon_ctl.name, icon_ctl.color = ft.Icons.ERROR_OUTLINE, Colors.DANGER
                text_ctl.color = Colors.DANGER_DARK
            else:
                icon_ctl.name, icon_ctl.color = ft.Icons.RADIO_BUTTON_UNCHECKED, Colors.TEXT_FAINT
                text_ctl.color = Colors.TEXT_MUTED
        try:
            controls["root"].update()
        except Exception:
            pass

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
