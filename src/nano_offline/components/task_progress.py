"""Shared progress section for the app's long operations.

One component drives the restore flow, the backup-creation window and the
exit-backup window so they share exactly the same visual language --
gradient header (built by the caller), percentage, step checklist, live
detail and speed/ETA. ``orientation`` only changes the arrangement:

* ``vertical``   -- ring + big percentage over a vertical step list (the
  focused, multi-stage restore / backup creation journeys).
* ``horizontal`` -- wide percentage row over a horizontal step strip (the
  quick exit prompt).

The controller exposes a plain ``controls`` dict so existing screens can keep
their own references (e.g. ``self._controls["percent"]``).
"""

from __future__ import annotations

import flet as ft

from nano_offline.core.theme import Colors, Radius

# Unified wording for the backup pipeline shared by create/exit/restore sub-steps.
BACKUP_STEPS = [
    ("snapshot", "لقطة القاعدة", ft.Icons.SAVE_OUTLINED),
    ("verify", "التحقق والبصمة", ft.Icons.VERIFIED_OUTLINED),
    ("pack", "الضغط", ft.Icons.FOLDER_ZIP),
    ("prune", "تنظيف النسخ", ft.Icons.CLEANING_SERVICES),
]


class ProgressSection:
    def __init__(self, *, orientation: str = "vertical", steps: list | None = None) -> None:
        self.orientation = "horizontal" if orientation == "horizontal" else "vertical"
        self.step_defs = list(steps or [])
        self.controls: dict = {}
        self.control = self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> ft.Control:
        c = self.controls
        c["percent"] = ft.Text("0%", size=30 if self.orientation == "vertical" else 24,
                               weight=ft.FontWeight.BOLD, color=Colors.PRIMARY)
        c["ring"] = ft.ProgressRing(
            width=64, height=64, stroke_width=6, value=None,
            color=Colors.PRIMARY, bgcolor=Colors.PRIMARY_BG,
        )
        c["bar"] = ft.ProgressBar(value=None, height=8, color=Colors.PRIMARY,
                                  bgcolor=Colors.PRIMARY_BG, border_radius=8)
        c["stage"] = ft.Text("جارٍ التحضير...", size=13, weight=ft.FontWeight.W_700,
                             color=Colors.TEXT_PRIMARY)
        c["detail"] = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
        c["metric"] = ft.Text("", size=10.5, color=Colors.TEXT_FAINT)
        c["live"] = ft.ProgressRing(width=14, height=14, stroke_width=2, color=Colors.PRIMARY, visible=False)

        step_controls: dict[str, dict] = {}
        if self.orientation == "vertical":
            rows = []
            for step_def in self.step_defs:
                key, label, icon = step_def[0], step_def[1], step_def[2]
                sub = step_def[3] if len(step_def) > 3 else ""
                icon_ctl = ft.Icon(icon, size=16, color=Colors.TEXT_FAINT)
                title = ft.Text(label, size=11.5, weight=ft.FontWeight.W_600, color=Colors.TEXT_MUTED)
                sub_ctl = ft.Text(sub, size=9.5, color=Colors.TEXT_FAINT, visible=bool(sub))
                metric = ft.Text("", size=10.5, color=Colors.TEXT_FAINT)
                row = ft.Container(
                    ft.Row([icon_ctl, ft.Column([title, sub_ctl], spacing=0, expand=True, tight=True), metric],
                           spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.padding.symmetric(horizontal=8, vertical=5), border_radius=Radius.SM,
                )
                step_controls[key] = {"container": row, "icon": icon_ctl, "title": title, "sub": sub_ctl, "metric": metric}
                rows.append(row)
            steps_control = ft.Column(rows, spacing=2)
            head = ft.Row(
                [c["ring"], ft.Column([c["percent"], ft.Text("نسبة الإنجاز", size=10, color=Colors.TEXT_SECONDARY)],
                                      spacing=0, tight=True),
                 ft.Container(expand=True), c["live"]],
                spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            self.control = ft.Column([head, c["bar"], ft.Column([c["stage"], c["detail"], c["metric"]], spacing=2, tight=True), steps_control],
                                     spacing=12)
        else:
            chips = []
            for step_def in self.step_defs:
                key, label, icon = step_def[0], step_def[1], step_def[2]
                icon_ctl = ft.Icon(icon, size=15, color=Colors.TEXT_FAINT)
                title = ft.Text(label, size=10.5, color=Colors.TEXT_MUTED)
                chip = ft.Container(
                    ft.Row([icon_ctl, title], spacing=6, tight=True),
                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                    border=ft.border.all(1, Colors.BORDER), border_radius=12, bgcolor=Colors.WHITE,
                )
                step_controls[key] = {"container": chip, "icon": icon_ctl, "title": title, "metric": None}
                chips.append(chip)
            self.control = ft.Column(
                [ft.Row([c["percent"], ft.Container(expand=True), c["live"], c["metric"]], spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER),
                 c["bar"], c["stage"], c["detail"], ft.Row(chips, spacing=8, wrap=True)],
                spacing=10,
            )
        c["steps"] = step_controls
        return self.control

    # ----------------------------------------------------------------- render
    def render(
        self,
        *,
        ratio: float = 0.0,
        indeterminate: bool = False,
        stage_key: str = "",
        title: str = "",
        detail: str = "",
        metric: str = "",
        states: dict | None = None,
    ) -> None:
        c = self.controls
        try:
            c["percent"].value = f"{int(round(max(0.0, min(1.0, float(ratio))) * 100))}%"
            if c.get("ring") is not None:
                c["ring"].value = None if indeterminate else max(0.0, min(1.0, float(ratio)))
            c["bar"].value = None if indeterminate else max(0.0, min(1.0, float(ratio)))
        except Exception:
            pass
        if title:
            c["stage"].value = title
        c["detail"].value = detail
        c["metric"].value = metric
        order = [step_def[0] for step_def in self.step_defs]
        current = states or {}
        idx = order.index(stage_key) if stage_key in order else 0
        for i, key in enumerate(order):
            row = c["steps"].get(key)
            if row is None:
                continue
            state = current.get(key) or ("done" if i < idx else "current" if i == idx else "pending")
            icon, color, text_color = {
                "done": (ft.Icons.CHECK_CIRCLE_ROUNDED, Colors.SUCCESS, Colors.SUCCESS_DARK),
                "current": (ft.Icons.SYNC_ROUNDED, Colors.PRIMARY, Colors.PRIMARY),
                "error": (ft.Icons.ERROR_OUTLINE, Colors.DANGER, Colors.DANGER_DARK),
                "pending": (ft.Icons.RADIO_BUTTON_UNCHECKED, Colors.TEXT_FAINT, Colors.TEXT_MUTED),
            }.get(str(state), (ft.Icons.RADIO_BUTTON_UNCHECKED, Colors.TEXT_FAINT, Colors.TEXT_MUTED))
            row["icon"].name, row["icon"].color = icon, color
            row["title"].color = text_color
        try:
            self.control.update()
        except Exception:
            pass


__all__ = ["ProgressSection", "BACKUP_STEPS"]
