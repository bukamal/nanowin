"""Advanced, horizontal "initialize database" window (Admin -> Backup).

Same visual language as the restore/import flow (gradient header, real step
progress, live detail, success summary), but laid out as a wide landscape
window with the password gate beside the live progress instead of a tall
stacked dialog.
"""

from __future__ import annotations

import asyncio
import threading

import flet as ft

from nano_offline.core.cancellation import OperationCancelled
from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.components.task_progress import ProgressSection

_INIT_STEPS = [
    ("safety", "نسخة أمان", ft.Icons.SHIELD_OUTLINED),
    ("build", "قاعدة جديدة", ft.Icons.CREATE_NEW_FOLDER_OUTLINED),
    ("swap", "التبديل", ft.Icons.SWAP_HORIZ_ROUNDED),
    ("finalize", "الجاهزية", ft.Icons.LOGIN_ROUNDED),
]
_INIT_STEP_KEYS = [key for key, _l, _i in _INIT_STEPS]
_STAGE_STEP = {
    "safety_backup": "safety",
    "integrity": "safety",
    "backup_hash": "safety",
    "backup_zip": "safety",
    "cleanup": "safety",
    "candidate": "build",
    "swap": "swap",
    "finalize": "finalize",
}
_STAGE_RANGE = {
    "safety_backup": (0.0, 0.42),
    "integrity": (0.42, 0.46),
    "backup_hash": (0.46, 0.50),
    "backup_zip": (0.50, 0.56),
    "cleanup": (0.56, 0.60),
    "candidate": (0.60, 0.80),
    "swap": (0.80, 0.90),
    "finalize": (0.90, 1.0),
}


class InitDatabaseFlow:
    """Owns the single 'تهيئة قاعدة البيانات' dialog for one AdminCenter."""

    def __init__(self, page, ctx, *, verify_password, notify, on_done):
        self.page = page
        self.ctx = ctx
        self.verify_password = verify_password
        self.notify = notify
        self.on_done = on_done
        self._active = False
        self._dialog = None
        self._controls = {}
        self._state = {}
        self._cancel = threading.Event()

    @property
    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------ open
    def open(self) -> bool:
        if self._active:
            self.notify("تهيئة قاعدة البيانات قيد التنفيذ بالفعل", kind="warning")
            return False
        self._active = True
        self._state = {
            "phase": "gate",
            "ratio": 0.0,
            "step": "safety",
            "stage": "safety_backup",
            "detail": "أدخل كلمة مرور المدير ثم أكّد التهيئة.",
            "error": None,
            "safety_name": "",
        }
        self._build()
        self.page.open(self._dialog)
        return True

    # --------------------------------------------------------------- visuals
    def _build(self) -> None:
        c = self._controls
        c["password"] = ft.TextField(
            label="كلمة مرور المدير", password=True, can_reveal_password=True,
            autofocus=True, dense=True,
        )
        c["status"] = ft.Text("", size=11, color=Colors.DANGER_DARKER)

        c["progress"] = ProgressSection(orientation="horizontal", steps=_INIT_STEPS)
        c.update(c["progress"].controls)
        c["progress_section"] = ft.Container(c["progress"].control, visible=False)

        c["idle"] = ft.Container(
            ft.Column([
                ft.Container(ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=30, color=Colors.WARNING_DARKER),
                             width=60, height=60, alignment=ft.alignment.center,
                             bgcolor=Colors.WARNING_BG, border_radius=18),
                ft.Text("تهيئة كاملة لقاعدة البيانات", size=13, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                ft.Text("تُنشأ نسخة أمان أولًا، ثم تُستبدل القاعدة بقاعدة فارغة نظيفة.",
                        size=11, color=Colors.TEXT_SECONDARY, text_align=ft.TextAlign.CENTER),
            ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=16, bgcolor=Colors.BACKGROUND_ALT, border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.MD, alignment=ft.alignment.center, expand=True, visible=True,
        )

        c["success_safety"] = ft.Text("", size=11, color=Colors.TEXT_SECONDARY, selectable=True)
        c["success"] = ft.Column([
            ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, size=40, color=Colors.SUCCESS),
                    ft.Column([ft.Text("تمت تهيئة قاعدة البيانات", size=15, weight=ft.FontWeight.BOLD, color=Colors.SUCCESS_DARK),
                               ft.Text("أُنشئت قاعدة فارغة بنجاح — نسخة الأمان:", size=11, color=Colors.TEXT_SECONDARY),
                               c["success_safety"]], spacing=2, expand=True, tight=True)],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, visible=False)

        c["error_text"] = ft.Text("", size=11.5, color=Colors.DANGER_DARK, selectable=True)
        c["error"] = ft.Column([
            ft.Row([ft.Icon(ft.Icons.ERROR_OUTLINE_ROUNDED, size=34, color=Colors.DANGER),
                    ft.Column([ft.Text("تعذّرت التهيئة", size=14, weight=ft.FontWeight.BOLD, color=Colors.DANGER_DARK),
                               c["error_text"]], spacing=2, expand=True, tight=True)],
                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=10, visible=False)

        c["confirm_btn"] = ft.FilledButton("تهيئة قاعدة البيانات", icon=ft.Icons.RESTART_ALT,
                                           on_click=lambda _=None: self._confirm())
        c["retry_btn"] = ft.OutlinedButton("إعادة المحاولة", icon=ft.Icons.REFRESH,
                                           on_click=lambda _=None: self._confirm(), visible=False)
        c["continue_btn"] = ft.FilledButton("متابعة", icon=ft.Icons.LOGIN_ROUNDED,
                                            on_click=lambda _=None: self._finish(), visible=False)
        c["cancel_btn"] = ft.TextButton("إلغاء", on_click=lambda _=None: self._close())

        header = ft.Container(
            ft.Row([
                ft.Container(ft.Icon(ft.Icons.RESTART_ALT, color=Colors.WHITE, size=22),
                             width=44, height=44, alignment=ft.alignment.center,
                             bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE), border_radius=14),
                ft.Column([
                    ft.Text("تهيئة قاعدة البيانات", size=17, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                    ft.Text("استبدال القاعدة بقاعدة فارغة نظيفة مع الاحتفاظ بترخيص الجهاز",
                            size=11, color=ft.Colors.with_opacity(0.9, ft.Colors.WHITE)),
                ], spacing=1, expand=True, tight=True),
            ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.symmetric(horizontal=16, vertical=14),
            gradient=ft.LinearGradient(colors=[Colors.BRAND_START, Colors.BRAND_CYAN],
                                       begin=ft.alignment.center_left, end=ft.alignment.center_right),
            border_radius=ft.border_radius.only(top_left=18, top_right=18),
        )

        warning = ft.Container(
            ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=18, color=Colors.DANGER_DARKER),
                ft.Text("سيتم حذف المستخدمين والمواد والفواتير والعملاء والموردين والإعدادات. "
                        "تُحفظ نسخة أمان كاملة قبل ذلك.", size=11, color=Colors.DANGER_DARKER, expand=True),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.symmetric(horizontal=14, vertical=10),
            bgcolor=Colors.DANGER_BG, border=ft.border.all(1, Colors.DANGER), border_radius=Radius.MD,
        )

        c["gate_col"] = ft.Column([
            c["password"], c["status"],
            ft.Row([c["confirm_btn"], c["retry_btn"]], spacing=8),
        ], spacing=12, expand=True)
        c["work_col"] = ft.Column([
            c["idle"], c["progress_section"], c["success"], c["error"], c["continue_btn"],
        ], spacing=10, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        try:
            available = int(getattr(self.page, "width", 0) or 0)
        except Exception:
            available = 0
        width = 780 if available <= 0 else max(360, min(780, available - 48))

        c["cols"] = (
            ft.Row([c["gate_col"], c["work_col"]], spacing=18,
                   vertical_alignment=ft.CrossAxisAlignment.START, expand=True)
            if width >= 660
            else ft.Column([c["gate_col"], c["work_col"]], spacing=14)
        )
        body = ft.Container(
            ft.Column([warning, c["cols"]], spacing=12),
            width=width, padding=ft.padding.only(left=16, right=16, top=14, bottom=6),
        )
        root = ft.Container(
            ft.Column([header, body,
                       ft.Container(ft.Row([c["cancel_btn"]], alignment=ft.MainAxisAlignment.END),
                                    padding=ft.padding.only(left=16, right=16, top=0, bottom=12))],
                      spacing=0),
            width=width, bgcolor=Colors.WHITE, border_radius=18, shadow=Shadow.LG, rtl=True,
        )
        self._controls["root"] = root
        self._dialog = ft.AlertDialog(modal=True, content=root, content_padding=0,
                                      on_dismiss=lambda _e: self._on_dismissed())
        self._render()

    # --------------------------------------------------------------- render
    def _render(self) -> None:
        c = self._controls
        if not c:
            return
        phase = str(self._state.get("phase") or "gate")
        running = phase == "running"
        done = phase == "done"
        errored = phase == "error"

        c["idle"].visible = phase == "gate"
        c["progress_section"].visible = running
        c["success"].visible = done
        c["error"].visible = errored
        c["password"].disabled = running or done
        c["confirm_btn"].visible = phase == "gate"
        c["retry_btn"].visible = errored
        c["continue_btn"].visible = done
        c["cancel_btn"].visible = not done
        if errored:
            c["error_text"].value = str(self._state.get("error") or "")
        try:
            c["root"].update()
        except Exception:
            pass

    def _render_progress(self) -> None:
        progress = self._controls.get("progress")
        if progress is None:
            return
        keys = _INIT_STEP_KEYS
        step = str(self._state.get("step") or "safety")
        idx = keys.index(step) if step in keys else 0
        if self._state.get("phase") == "done":
            states = {k: "done" for k in keys}
        elif self._state.get("phase") == "error":
            states = {k: ("done" if i < idx else "error" if i == idx else "pending")
                      for i, k in enumerate(keys)}
        else:
            states = None
        title = next((label for key, label, _i in _INIT_STEPS if key == step), "جارٍ العمل...")
        progress.render(
            ratio=float(self._state.get("ratio") or 0.0),
            indeterminate=bool(self._state.get("indeterminate", True)) and self._state.get("phase") != "done",
            stage_key=step,
            title=title,
            detail=str(self._state.get("detail") or ""),
            metric=str(self._state.get("metric") or ""),
            states=states,
        )

    # --------------------------------------------------------------- actions
    def _confirm(self) -> None:
        if self._state.get("phase") == "running":
            return
        password = self._controls["password"].value or ""
        try:
            self.verify_password(password)
        except Exception as exc:
            self._controls["status"].value = str(exc)
            try:
                self._controls["root"].update()
            except Exception:
                pass
            return
        self._controls["status"].value = ""
        self._state.update({"phase": "running", "ratio": 0.0, "step": "safety",
                            "stage": "safety_backup", "detail": "جارٍ إنشاء نسخة الأمان...",
                            "error": None, "indeterminate": True, "metric": ""})
        self._render()
        self.page.run_task(self._pump)
        self.page.run_task(self._execute)

    def _progress_cb(self, stage, done, total, unit, detail=""):
        try:
            stage = str(stage)
            step = _STAGE_STEP.get(stage, self._state.get("step", "safety"))
            lo, hi = _STAGE_RANGE.get(stage, (0.0, 1.0))
            ratio = lo + (hi - lo) * min(float(done) / float(total), 1.0) if total and float(total) > 0 else lo
            metric = ""
            if unit == "bytes" and total and float(total) > 0:
                metric = f"{float(done) / 1e6:.1f} / {float(total) / 1e6:.1f} MB"
            self._state.update({
                "stage": stage, "step": step, "ratio": max(0.0, min(0.99, ratio)),
                "detail": detail or self._state.get("detail", ""),
                "indeterminate": not (total and float(total) > 0), "metric": metric,
            })
        except Exception:
            pass

    async def _pump(self) -> None:
        while self._active and self._state.get("phase") == "running":
            self._render_progress()
            await asyncio.sleep(0.1)
        self._render_progress()

    async def _execute(self) -> None:
        try:
            safety_path = await asyncio.to_thread(
                self.ctx.backup.initialize_fresh_database, self._progress_cb, self._cancel
            )
            self._state.update({"detail": "إعادة تجهيز الواجهة...", "indeterminate": True, "step": "finalize"})
            self._render_progress()
            await asyncio.to_thread(self.ctx.reload, self.ctx.database_path())
            self._controls["success_safety"].value = getattr(safety_path, "name", str(safety_path))
            self._state.update({"phase": "done", "ratio": 1.0, "indeterminate": False,
                                "step": "finalize", "detail": "اكتملت التهيئة"})
        except OperationCancelled:
            self._state.update({"phase": "error", "error": "تم الإلغاء — لم تتغير قاعدة البيانات.",
                                "indeterminate": False})
        except Exception as exc:
            self._state.update({"phase": "error", "error": str(exc), "indeterminate": False})
        self._render()
        self._render_progress()

    def _finish(self) -> None:
        self._close()
        try:
            self.on_done()
        except Exception:
            pass

    def _on_dismissed(self) -> None:
        if self._state.get("phase") == "running":
            self._cancel.set()
        self._active = False

    def _close(self) -> None:
        self._active = False
        try:
            if self._dialog is not None:
                self.page.close(self._dialog)
        except Exception:
            pass


__all__ = ["InitDatabaseFlow"]
