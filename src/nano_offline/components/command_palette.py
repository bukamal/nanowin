from __future__ import annotations

from collections.abc import Callable

import flet as ft

from nano_offline.components.text_field import SelectAllTextField
from nano_offline.core.theme import Colors


class CommandPalette:
    """Keyboard-navigable quick switcher for the desktop shell."""

    def __init__(
        self,
        page: ft.Page,
        entries: list[tuple[str, str, str]],
        on_select: Callable[[str], None],
    ):
        self.page = page
        self.entries = entries
        self.on_select = on_select
        self.dialog: ft.AlertDialog | None = None
        self.results: ft.Column | None = None
        self.matches: list[tuple[str, str, str]] = []
        self.rows: list[ft.Container] = []
        self.selected = 0

    @property
    def is_open(self) -> bool:
        return self.dialog is not None

    def open(self, _=None) -> None:
        results = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, height=320)
        rows: list[ft.Container] = []
        field = SelectAllTextField(label="ابحث عن قسم…", autofocus=True)
        dialog = ft.AlertDialog(modal=True, title=ft.Text("بحث سريع"))

        def render_results(query: str = "") -> None:
            q = (query or "").strip().casefold()
            self.matches = [
                entry
                for entry in self.entries
                if not q or q in entry[1].casefold() or q in entry[0].casefold()
            ]
            self.selected = 0
            rows.clear()
            results.controls = []
            for index, (key, label, icon) in enumerate(self.matches):
                row = ft.Container(
                    ft.Row(
                        [
                            ft.Icon(icon, size=18, color=Colors.PRIMARY),
                            ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=Colors.TEXT_PRIMARY),
                        ],
                        spacing=12,
                    ),
                    padding=ft.padding.symmetric(horizontal=12, vertical=11),
                    border_radius=12,
                    bgcolor=Colors.PRIMARY_BG if index == 0 else None,
                    ink=True,
                    on_click=lambda _, route_key=key: self._select(route_key),
                )
                rows.append(row)
                results.controls.append(row)
            if not results.controls:
                results.controls.append(ft.Text("لا نتائج مطابقة", size=12, color=Colors.TEXT_SECONDARY))
            if results.page is not None:
                results.update()

        field.on_change = lambda _: render_results(field.value)
        render_results()
        dialog.content = ft.Container(ft.Column([field, results], spacing=12, tight=True), width=440)
        self.dialog = dialog
        self.results = results
        self.rows = rows
        self.page.open(dialog)

    def handle_key(self, key: str) -> bool:
        """Handle palette-only keys; return False when the palette is closed."""
        if not self.is_open:
            return False
        if key in {"escape", "esc"}:
            self._close()
        elif key in {"arrowdown", "down", "arrowup", "up"}:
            if self.matches:
                step = 1 if key in {"arrowdown", "down"} else -1
                self.selected = (self.selected + step) % len(self.matches)
                for index, row in enumerate(self.rows):
                    row.bgcolor = Colors.PRIMARY_BG if index == self.selected else None
                if self.results is not None and self.results.page is not None:
                    self.results.update()
        elif key in {"enter", "numpadenter"} and self.matches:
            self._select(self.matches[self.selected][0])
        else:
            return True
        return True

    def _select(self, key: str) -> None:
        self._close()
        self.on_select(key)

    def _close(self) -> None:
        dialog = self.dialog
        self.dialog = None
        self.results = None
        self.rows = []
        self.matches = []
        self.selected = 0
        if dialog is not None:
            self.page.close(dialog)


__all__ = ["CommandPalette"]
