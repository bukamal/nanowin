"""Column show/hide state used by the DataGrid-backed tables."""

from __future__ import annotations

import flet as ft

from nano_offline.components.column_visibility import ColumnVisibility
from nano_offline.components.data_grid import DataGrid
from nano_offline.components.grid_model import GridColumn


class FakeSettings:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set(self, key: str, value: str | None) -> None:
        if value is None or value == "":
            self.values.pop(key, None)
        else:
            self.values[key] = str(value)


COLS = [
    GridColumn("name", "الاسم"),
    GridColumn("qty", "الكمية", kind="number"),
    GridColumn("price", "السعر", kind="money"),
]


def test_column_visibility_persists_and_refreshes_menu_checkmarks():
    settings = FakeSettings()
    changes: list[set[str]] = []
    state = ColumnVisibility(
        columns=COLS,
        settings=settings,
        preference_key="ui.test_hidden_columns",
        on_change=lambda: changes.append(state.hidden_keys),
    )
    button = state.button()

    assert [item.checked for item in button.items] == [True, True, True]

    state.toggle("qty")

    assert state.hidden_keys == {"qty"}
    assert settings.get("ui.test_hidden_columns") == "qty"
    assert changes == [{"qty"}]
    assert [item.checked for item in button.items] == [True, False, True]


def test_column_visibility_loads_saved_keys_and_ignores_stale_ones():
    settings = FakeSettings({"ui.test_hidden_columns": "price,removed"})
    state = ColumnVisibility(
        columns=COLS,
        settings=settings,
        preference_key="ui.test_hidden_columns",
    )

    assert state.hidden_keys == {"price"}
    assert state.is_visible("name") is True
    assert state.is_visible("price") is False


def test_auto_hidden_columns_are_reported_but_not_saved_as_user_preferences():
    settings = FakeSettings()
    state = ColumnVisibility(
        columns=COLS,
        settings=settings,
        preference_key="ui.test_hidden_columns",
        auto_hidden={"price"},
    )

    assert state.hidden_keys == {"price"}
    assert state.is_visible("price") is False
    assert settings.get("ui.test_hidden_columns") == ""


def test_datagrid_accepts_and_updates_hidden_keys():
    grid = DataGrid(columns=COLS, rows=[], hidden_keys={"qty"})

    assert grid.visible_keys() == ["name", "price"]

    grid.set_hidden_keys({"name"})

    assert grid.visible_keys() == ["qty", "price"]


def test_column_menu_button_has_the_expected_icon_and_tooltip():
    state = ColumnVisibility(
        columns=COLS,
        settings=FakeSettings(),
        preference_key="ui.test_hidden_columns",
    )
    button = state.button()

    assert isinstance(button, ft.PopupMenuButton)
    assert button.icon == ft.Icons.VIEW_COLUMN
    assert button.tooltip == "إظهار/إخفاء الأعمدة"
