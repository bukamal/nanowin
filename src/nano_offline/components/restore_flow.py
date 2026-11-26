"""One unified, live restore/import flow window.

Restoring a large backup is not a single opaque wait: it copies the archive,
extracts it, verifies the SHA-256 + SQLite integrity, creates a safety copy,
migrates the candidate, swaps the live DB, then rebuilds all derived
accounting/inventory state. This module turns that whole pipeline into one
single dialog that never re-opens (no duplicate windows) and reports precise,
entertaining progress -- percentages, live counts, transfer speed and ETA --
with a cooperative cancel that stays available until the swap is irreversible.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import flet as ft

from nano_offline.core.cancellation import OperationCancelled
from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.components.task_progress import ProgressSection

_STEPS = [
    ("source", "الملف", "اختيار ونسخ النسخة"),
    ("verify", "التحقق", "فك الضغط والبصمة والسلامة"),
    ("safety", "نسخة أمان", "نسخ قاعدة البيانات الحالية"),
    ("prepare", "التجهيز", "ترحيل بنية قاعدة الاسترجاع"),
    ("swap", "التبديل", "استبدال قاعدة البيانات"),
    ("rebuild", "إعادة البناء", "المخزون والدفعات والقيود"),
    ("finish", "الجاهزية", "تجهيز القاعدة والترخيص"),
]
_STEP_KEYS = [key for key, _title, _sub in _STEPS]
_STEP_ICONS = {
    "source": ft.Icons.UPLOAD_FILE_OUTLINED,
    "verify": ft.Icons.VERIFIED_OUTLINED,
    "safety": ft.Icons.SHIELD_OUTLINED,
    "prepare": ft.Icons.BUILD_OUTLINED,
    "swap": ft.Icons.SWAP_HORIZ_ROUNDED,
    "rebuild": ft.Icons.AUTORENEW_ROUNDED,
    "finish": ft.Icons.LOGIN_ROUNDED,
}
_STEP_RANGES = {
    "source": (0.00, 0.08),
    "verify": (0.08, 0.26),
    "safety": (0.26, 0.40),
    "prepare": (0.40, 0.50),
    "swap": (0.50, 0.54),
    "rebuild": (0.54, 0.98),
    "finish": (0.98, 1.00),
}
_REBUILD_RANGES = {
    "rebuild_inventory": (0.54, 0.78),
    "rebuild_initial_payments": (0.78, 0.81),
    "rebuild_ledger_invoices": (0.81, 0.90),
    "rebuild_ledger_payments": (0.90, 0.97),
    "rebuild_party": (0.97, 0.98),
}
_STAGE_STEP = {
    "import_copy": "source",
    "extract": "verify",
    "hash": "verify",
    "integrity": "verify",
    "safety_backup": "safety",
    "backup_hash": "safety",
    "backup_zip": "safety",
    "prepare": "prepare",
    "candidate_migrate": "prepare",
    "swap": "swap",
    "finalize": "finish",
}
for _stage in _REBUILD_RANGES:
    _STAGE_STEP[_stage] = "rebuild"

_STAGE_TITLES = {
    "import_copy": "نسخ ملف النسخة الاحتياطية",
    "extract": "فك ضغط قاعدة البيانات",
    "hash": "التحقق من البصمة",
    "integrity": "فحص سلامة قاعدة البيانات",
    "safety_backup": "إنشاء نسخة أمان",
    "backup_hash": "بصمة نسخة الأمان",
    "backup_zip": "ضغط نسخة الأمان",
    "prepare": "تحضير قاعدة الاسترجاع",
    "candidate_migrate": "ترحيل بنية القاعدة",
    "swap": "تبديل قاعدة البيانات",
    "rebuild_inventory": "إعادة بناء حركات المخزون",
    "rebuild_initial_payments": "مزامنة الدفعات الأولية",
    "rebuild_ledger_invoices": "ترحيل قيود الفواتير",
    "rebuild_ledger_payments": "ترحيل قيود الدفعات والسندات",
    "rebuild_party": "تحديث أرصدة الأطراف",
    "finalize": "تجهيز قاعدة البيانات",
}


def _human_bytes(value: float) -> str:
    value = float(value or 0)
    if value < 1024:
        return f"{value:.0f} B"
    kb = value / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


class RestoreFlow:
    """Owns the single restore/import dialog for one AdminCenter."""

    def __init__(self, page, ctx, native_files, *, notify, on_logout):
        self.page = page
        self.ctx = ctx
        self.native_files = native_files
        self.notify = notify
        self.on_logout = on_logout
        self._active = False
        self._dialog = None
        self._state = {}
        self._cancel = threading.Event()
        self._controls = {}

    @property
    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------ open
    def open(self, source: Path | str, *, external_name: str | None = None, copy_source: bool = False) -> bool:
        if self._active:
            self.notify("يوجد استيراد/استرجاع قيد التنفيذ بالفعل", kind="warning")
            return False
        source = Path(source)
        self._active = True
        self._cancel = threading.Event()
        self._state = {
            "stage": "import_copy" if copy_source else "verify",
            "step": "source" if copy_source else "verify",
            "done": 0.0,
            "total": 0.0,
            "unit": "",
            "detail": "بدء العملية...",
            "ratio": 0.0,
            "indeterminate": True,
            "critical": False,
            "finished": False,
            "ok": False,
            "error": None,
            "cancelled": False,
            "summary": None,
            "started": time.monotonic(),
            "speed": None,
            "eta": None,
            "_last_done": 0.0,
            "_last_time": time.monotonic(),
        }
        self._build_dialog(source, external_name, copy_source)
        self.page.open(self._dialog)
        self.page.run_task(self._pump)
        self.page.run_task(self._execute, source, external_name, copy_source)
        return True

    # --------------------------------------------------------------- visuals
    def _build_dialog(self, source: Path, external_name: str | None, copy_source: bool) -> None:
        title = "استيراد نسخة احتياطية" if copy_source else "استرجاع نسخة احتياطية"
        self._controls["file_name"] = ft.Text(
            external_name or source.name, size=12, weight=ft.FontWeight.W_600,
            color=Colors.TEXT_PRIMARY, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._controls["file_meta"] = ft.Text(
            _human_bytes(source.stat().st_size) if source.exists() else "",
            size=11, color=Colors.TEXT_SECONDARY,
        )
        self._controls["file_path"] = ft.Text(
            str(source), size=10, color=Colors.TEXT_FAINT, selectable=True,
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._progress = ProgressSection(
            orientation="vertical",
            steps=[(key, label, _STEP_ICONS.get(key, ft.Icons.CIRCLE_OUTLINED), sub) for key, label, sub in _STEPS],
        )
        self._controls.update(self._progress.controls)
        progress_section = self._progress.control

        self._controls["cancel"] = ft.OutlinedButton(
            "إلغاء",
            icon=ft.Icons.CLOSE,
            on_click=lambda _=None: self._request_cancel(),
            style=ft.ButtonStyle(color=Colors.DANGER_DARK),
        )
        self._controls["hint"] = ft.Text(
            "يمكنك الإلغاء بأمان حتى لحظة تبديل قاعدة البيانات. بياناتك الحالية محفوظة دائمًا في نسخة أمان.",
            size=9.5, color=Colors.TEXT_FAINT,
        )

        self._controls["success_icon"] = ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, size=54, color=Colors.SUCCESS)
        self._controls["success_title"] = ft.Text("تم الاسترجاع بنجاح", size=17, weight=ft.FontWeight.BOLD, color=Colors.SUCCESS_DARK)
        self._controls["summary"] = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
        self._controls["success"] = ft.Column(
            [
                self._controls["success_icon"],
                self._controls["success_title"],
                self._controls["summary"],
                ft.FilledButton(
                    "متابعة",
                    icon=ft.Icons.LOGIN_ROUNDED,
                    on_click=lambda _=None: self._finish_success(),
                ),
            ],
            spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER, visible=False,
        )
        self._controls["error_icon"] = ft.Icon(ft.Icons.ERROR_OUTLINE_ROUNDED, size=48, color=Colors.DANGER)
        self._controls["error_text"] = ft.Text("", size=11.5, color=Colors.DANGER_DARK, selectable=True)
        self._controls["error"] = ft.Column(
            [
                self._controls["error_icon"],
                ft.Text("تعذّر إكمال العملية", size=16, weight=ft.FontWeight.BOLD, color=Colors.DANGER_DARK),
                self._controls["error_text"],
                ft.TextButton("إغلاق", on_click=lambda _=None: self._close()),
            ],
            spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER, visible=False,
        )

        header = ft.Container(
            ft.Row(
                [
                    ft.Container(
                        ft.Icon(ft.Icons.SETTINGS_BACKUP_RESTORE_ROUNDED, color=Colors.WHITE, size=22),
                        width=44, height=44, alignment=ft.alignment.center,
                        bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE),
                        border_radius=14,
                    ),
                    ft.Column(
                        [
                            ft.Text(title, size=17, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                            self._controls["file_name"],
                        ],
                        spacing=1, expand=True, tight=True,
                    ),
                ],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=16, vertical=14),
            gradient=ft.LinearGradient(
                colors=[Colors.BRAND_START, Colors.BRAND_CYAN],
                begin=ft.alignment.center_left, end=ft.alignment.center_right,
            ),
            border_radius=ft.border_radius.only(top_left=18, top_right=18),
        )

        path_row = ft.Container(
            ft.Row(
                [
                    ft.Icon(ft.Icons.FOLDER_OPEN_OUTLINED, size=15, color=Colors.TEXT_SECONDARY),
                    ft.Column(
                        [
                            ft.Text("مسار الملف", size=9.5, color=Colors.TEXT_SECONDARY),
                            self._controls["file_path"],
                        ],
                        spacing=1, expand=True, tight=True,
                    ),
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=10, bgcolor=Colors.BACKGROUND_ALT,
            border=ft.border.all(1, Colors.BORDER), border_radius=Radius.MD,
        )

        root = ft.Container(
            ft.Column(
                [
                    header,
                    ft.Container(
                        ft.Column(
                            [path_row, progress_section, self._controls["success"], self._controls["error"]],
                            spacing=12,
                        ),
                        padding=ft.padding.only(left=16, right=16, top=14, bottom=8),
                    ),
                    ft.Container(
                        ft.Column(
                            [
                                self._controls["hint"],
                                ft.Row([self._controls["cancel"]], alignment=ft.MainAxisAlignment.END),
                            ],
                            spacing=6,
                        ),
                        padding=ft.padding.only(left=16, right=16, top=0, bottom=14),
                    ),
                ],
                spacing=0,
            ),
            width=560,
            bgcolor=Colors.WHITE,
            border_radius=18,
            shadow=Shadow.LG,
            rtl=True,
        )
        self._controls["root"] = root
        self._controls["progress_section"] = progress_section
        self._dialog = ft.AlertDialog(
            modal=True, content=root, content_padding=0,
            on_dismiss=lambda _e: self._on_dismissed(),
        )

    # -------------------------------------------------------------- progress
    def _handle_progress(self, stage: str, done: float, total: float, unit: str, detail: str = "") -> None:
        st = self._state
        now = time.monotonic()
        prev_done = float(st.get("_last_done") or 0)
        prev_time = float(st.get("_last_time") or now)
        dt = max(now - prev_time, 1e-6)
        speed = (float(done) - prev_done) / dt if done >= prev_done else None
        eta = None
        if speed and total and total > done:
            eta = (float(total) - float(done)) / speed
        st.update({
            "stage": stage, "done": float(done), "total": float(total),
            "unit": unit, "detail": detail or _STAGE_TITLES.get(stage, ""),
            "speed": speed, "eta": eta, "_last_done": float(done), "_last_time": now,
        })
        st["step"] = _STAGE_STEP.get(stage, st.get("step", "verify"))
        if stage == "swap":
            st["critical"] = True
        st["ratio"] = self._ratio_for(stage, float(done), float(total))
        st["indeterminate"] = bool(total <= 0)

    def _ratio_for(self, stage: str, done: float, total: float) -> float:
        if stage in _REBUILD_RANGES:
            lo, hi = _REBUILD_RANGES[stage]
        else:
            step = _STAGE_STEP.get(stage, "verify")
            lo, hi = _STEP_RANGES.get(step, (0.0, 1.0))
        if total <= 0:
            return lo
        return max(0.0, min(1.0, lo + (hi - lo) * min(done / total, 1.0)))

    def _request_cancel(self) -> None:
        if self._state.get("critical"):
            self.notify("لا يمكن الإلغاء بعد بدء تبديل قاعدة البيانات", kind="warning")
            return
        self._cancel.set()
        self._state["detail"] = "جارٍ الإلغاء بأمان..."
        try:
            self._controls["cancel"].disabled = True
            self._controls["cancel"].text = "جارٍ الإلغاء..."
            self._controls["cancel"].update()
        except Exception:
            pass

    async def _pump(self) -> None:
        while self._active:
            try:
                self._render()
            except Exception:
                pass
            if self._state.get("finished"):
                break
            await asyncio.sleep(0.12)
        try:
            self._render()
            self._after_finish()
        except Exception:
            pass

    def _render(self) -> None:
        st = self._state
        c = self._controls
        ratio = float(st.get("ratio") or 0.0)
        indeterminate = bool(st.get("indeterminate")) and not st.get("finished")
        stage = str(st.get("stage") or "")
        current_step = str(st.get("step") or "verify")
        current_index = _STEP_KEYS.index(current_step) if current_step in _STEP_KEYS else 0
        states = {}
        for index, key in enumerate(_STEP_KEYS):
            if st.get("finished") and st.get("ok"):
                states[key] = "done"
            elif index < current_index:
                states[key] = "done"
            elif index == current_index:
                states[key] = "error" if st.get("error") else (
                    "done" if st.get("finished") and st.get("ok") else "current"
                )
            else:
                states[key] = "pending"
            row = c["steps"].get(key)
            if row is not None and row.get("metric") is not None:
                row["metric"].value = self._step_metric(st, key)
        self._progress.render(
            ratio=ratio,
            indeterminate=indeterminate,
            stage_key=current_step,
            title=_STAGE_TITLES.get(stage, stage or "جارٍ التحضير..."),
            detail=str(st.get("detail") or ""),
            metric=self._metric_text(st),
            states=states,
        )
        try:
            c["cancel"].disabled = bool(st.get("critical") or st.get("finished"))
            c["cancel"].visible = not st.get("finished")
            c["live"].visible = not st.get("finished")
            c["root"].update()
        except Exception:
            pass

    @staticmethod
    def _format_count(value: float) -> str:
        return f"{int(value):,}"

    def _metric_text(self, st: dict) -> str:
        unit = str(st.get("unit") or "")
        done = float(st.get("done") or 0)
        total = float(st.get("total") or 0)
        bits = []
        if total > 0:
            if unit == "bytes":
                bits.append(f"{_human_bytes(done)} / {_human_bytes(total)}")
            else:
                bits.append(f"{self._format_count(done)} / {self._format_count(total)}")
        speed = st.get("speed")
        if speed:
            bits.append(f"{_human_bytes(speed)}/ث" if unit == "bytes" else f"{self._format_count(speed)}/ث")
        eta = st.get("eta")
        if eta is not None:
            bits.append(f"متبقٍ ~{int(max(0, eta))} ث")
        return " • ".join(bits)

    def _step_metric(self, st: dict, key: str) -> str:
        if key != st.get("step"):
            return ""
        unit = str(st.get("unit") or "")
        total = float(st.get("total") or 0)
        if total > 0:
            done = float(st.get("done") or 0)
            if unit == "bytes":
                return f"{_human_bytes(done)} / {_human_bytes(total)}"
            return f"{self._format_count(done)} / {self._format_count(total)}"
        return ""

    # --------------------------------------------------------------- execute
    async def _execute(self, source: Path, external_name: str | None, copy_source: bool) -> None:
        st = self._state
        target = source
        try:
            if copy_source:
                suffix = "".join(source.suffixes) or ".nanobackup"
                stem = Path(external_name or source.name).stem or "imported_backup"
                stamp = time.strftime("%Y%m%d_%H%M%S")
                target = self.ctx.backup.default_dir() / f"{stem[:80]}_{stamp}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    self.ctx.backup._copy_file, source, target,
                    self._handle_progress, self._cancel, stage="import_copy",
                )
                st["step"] = "verify"
                st["detail"] = "تم نسخ الملف، بدء التحقق"
            await asyncio.to_thread(
                self.ctx.backup.restore_backup,
                target,
                progress=self._handle_progress,
                cancel=self._cancel,
            )
            st["critical"] = True
            st["detail"] = "تم تبديل قاعدة البيانات، إعادة بناء الحسابات..."
            st["step"] = "rebuild"
            st["indeterminate"] = True
            try:
                self._controls["cancel"].disabled = True
                self._controls["cancel"].text = "لا يمكن الإلغاء الآن"
                self._controls["cancel"].update()
            except Exception:
                pass
            await asyncio.to_thread(
                self.ctx.reload,
                self.ctx.database_path(),
                progress=self._handle_progress,
                cancel=None,
            )
            st["step"] = "finish"
            st["ratio"] = 0.99
            st["detail"] = "تجهيز شاشة الدخول..."
            counts = self._counts()
            st.update({"ok": True, "finished": True, "ratio": 1.0, "indeterminate": False,
                       "summary": counts, "detail": "تمت العملية بنجاح"})
        except OperationCancelled:
            st.update({"finished": True, "ok": False, "cancelled": True,
                       "detail": "تم الإلغاء قبل تبديل قاعدة البيانات — بياناتك لم تتغير.",
                       "indeterminate": False})
            st["error"] = "أُلغي الاستيراد بأمان. لم يتم تبديل قاعدة البيانات الحالية."
        except Exception as exc:
            st.update({"finished": True, "ok": False, "error": str(exc),
                       "detail": "فشلت العملية", "indeterminate": False})
        finally:
            try:
                self._render()
            except Exception:
                pass

    def _counts(self) -> str:
        try:
            with self.ctx.db.connect() as conn:
                items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                invoices = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
                vouchers = conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0]
            return f"{items:,} مادة • {invoices:,} فاتورة • {vouchers:,} سند"
        except Exception:
            return ""

    def _finish_success(self) -> None:
        self._close()
        try:
            self.on_logout()
        except Exception:
            pass

    def _on_dismissed(self) -> None:
        # Back-gesture / outside dismissal must never leave the flow stuck as
        # "active" while its worker keeps running. Cancel cooperatively; the
        # worker then aborts before the swap.
        if not self._state.get("finished"):
            self._cancel.set()
        self._active = False

    def _close(self) -> None:
        self._active = False
        try:
            if self._dialog is not None:
                self.page.close(self._dialog)
        except Exception:
            pass

    # called by the pump once finished to switch visible section
    def _after_finish(self) -> None:
        st = self._state
        progress = self._controls.get("progress_section")
        success = self._controls.get("success")
        error = self._controls.get("error")
        if st.get("ok"):
            if progress is not None:
                progress.visible = False
            if success is not None:
                success.visible = True
            summary = self._controls.get("summary")
            if summary is not None:
                summary.value = str(st.get("summary") or "")
        elif st.get("error") is not None or st.get("cancelled"):
            if progress is not None:
                progress.visible = False
            if error is not None:
                error.visible = True
            text = self._controls.get("error_text")
            if text is not None:
                text.value = str(st.get("error") or "")
        try:
            self._controls["root"].update()
        except Exception:
            pass
