"""Reusable column show/hide state and menu for :class:`DataGrid` tables.

The grid already knows how to render a subset of its columns
(``hidden_keys`` / ``set_hidden_keys``); this module owns the small piece
every view was repeating: load the saved preference, toggle a key, write
it back, and keep the popup menu's checkmarks in sync.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import flet as ft

from nano_offline.components.grid_model import GridColumn


def parse_hidden_columns(raw: str | None) -> set[str]:
    """Parse the comma-separated setting format used by the table views."""
    return {part.strip() for part in str(raw or "").split(",") if part.strip()}


def save_hidden_columns(settings, key: str, keys: Iterable[str]) -> None:
    settings.set(key, ",".join(sorted(set(keys))))


class ColumnVisibility:
    """Column visibility state backed by one settings key.

    ``on_change`` is called after every toggle; the owning view uses it to
    push ``hidden_keys`` into its grid and refresh the page. The menu is
    rebuilt on each toggle so checkmarks reflect the new state without
    reopening the sheet.
    """

    def __init__(
        self,
        *,
        columns: Iterable[GridColumn],
        settings,
        preference_key: str,
        on_change: Callable[[], None] | None = None,
        auto_hidden: Iterable[str] | None = None,
        tooltip: str = "إظهار/إخفاء الأعمدة",
    ):
        self.columns = list(columns)
        self.settings = settings
        self.preference_key = preference_key
        self._on_change = on_change
        self._auto_hidden = set(auto_hidden or ())
        self._keys = parse_hidden_columns(settings.get(preference_key, ""))
        self._keys.intersection_update(col.key for col in self.columns)
        self._button: ft.PopupMenuButton | None = None
        self._tooltip = tooltip

    @property
    def hidden_keys(self) -> set[str]:
        """Keys hidden by the user, plus responsive/automatic exclusions."""
        return set(self._keys) | set(self._auto_hidden)

    def is_visible(self, key: str) -> bool:
        return key not in self._keys and key not in self._auto_hidden

    def toggle(self, key: str) -> None:
        if key in self._keys:
            self._keys.discard(key)
        else:
            self._keys.add(key)
        save_hidden_columns(self.settings, self.preference_key, self._keys)
        self._refresh_items()
        if self._on_change is not None:
            self._on_change()

    def button(self) -> ft.PopupMenuButton:
        self._button = ft.PopupMenuButton(
            icon=ft.Icons.VIEW_COLUMN,
            tooltip=self._tooltip,
            items=self._items(),
        )
        return self._button

    def _items(self) -> list[ft.PopupMenuItem]:
        return [
            ft.PopupMenuItem(
                text=col.label,
                checked=self.is_visible(col.key),
                on_click=lambda _, k=col.key: self.toggle(k),
            )
            for col in self.columns
        ]

    def _refresh_items(self) -> None:
        if self._button is None:
            return
        self._button.items = self._items()
        try:
            self._button.update()
        except Exception:
            # The menu can be toggled before it is attached (tests/headless).
            pass


__all__ = ["ColumnVisibility", "parse_hidden_columns", "save_hidden_columns"]
