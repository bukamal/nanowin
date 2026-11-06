"""Fit-to-width: DataGrid distributes column widths proportionally."""

from __future__ import annotations

from types import SimpleNamespace

from nano_offline.components.data_grid import _FIT_DEFAULT_WEIGHT, DataGrid
from nano_offline.components.grid_model import GridColumn
from nano_offline.core import grid_settings


class _Settings:
    def __init__(self, data=None):
        self._d = dict(data or {})

    def get(self, key, default=None):
        return self._d.get(key, default)


COLS = [GridColumn("a", "A", width=200), GridColumn("b", "B", width=None)]


def test_grid_fit_width_defaults_on_and_toggles():
    assert grid_settings.fit_width(_Settings()) is True
    assert grid_settings.fit_width(_Settings({grid_settings.FIT_WIDTH_KEY: "0"})) is False


def test_cell_kwargs_fit_on_uses_expand_weights():
    g = DataGrid(columns=COLS, rows=[{"a": 1, "b": 2}], page=None, fit_width=True)
    assert g._cell_kwargs(COLS[0]) == {"expand": 200}
    assert g._cell_kwargs(COLS[1]) == {"expand": _FIT_DEFAULT_WEIGHT}


def test_cell_kwargs_fit_off_keeps_fixed_widths():
    g = DataGrid(columns=COLS, rows=[{"a": 1, "b": 2}], page=None, fit_width=False)
    assert g._cell_kwargs(COLS[0]) == {"width": 200}
    assert g._cell_kwargs(COLS[1]) == {"expand": True}


def test_fit_width_resolved_from_page_setting():
    page = SimpleNamespace(_nano_ctx=SimpleNamespace(settings=_Settings({grid_settings.FIT_WIDTH_KEY: "0"})))
    g = DataGrid(columns=COLS, rows=[{"a": 1, "b": 2}], page=page)
    assert g._fit_width is False
