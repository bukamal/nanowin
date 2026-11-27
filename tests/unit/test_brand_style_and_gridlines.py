from nano_offline.core import grid_settings, theme
from nano_offline.components.data_grid import DataGrid, GridColumn


def test_normalize_brand_style():
    assert theme.normalize_brand_style("SOLID") == "solid"
    assert theme.normalize_brand_style("gradient") == "gradient"
    assert theme.normalize_brand_style("weird") == "gradient"
    assert theme.normalize_brand_style(None) == "gradient"


def test_brand_style_solid_flattens_every_gradient():
    try:
        theme.set_brand_style("solid")
        assert theme.get_brand_style() == "solid"
        for grad in (theme.brand_gradient(), theme.brand_gradient(soft=True),
                     theme.tile_gradient("#0B5FCC"), theme.surface_gradient()):
            assert grad.colors[0] == grad.colors[1]
    finally:
        theme.set_brand_style("gradient")


def test_brand_style_gradient_is_three_stops():
    theme.set_brand_style(None)
    assert len(theme.brand_gradient().colors) == 3
    assert len(theme.tile_gradient("#0B5FCC").colors) == 3
    theme.set_brand_style("gradient")


def test_vertical_lines_setting_defaults_on_and_toggles():
    settings: dict = {}
    assert grid_settings.vertical_lines(settings) is True
    settings[grid_settings.VERTICAL_LINES_KEY] = "0"
    assert grid_settings.vertical_lines(settings) is False
    settings[grid_settings.VERTICAL_LINES_KEY] = "1"
    assert grid_settings.vertical_lines(settings) is True


def test_data_grid_vertical_lines_draws_cell_borders():
    cols = [GridColumn("a", "A", width=80), GridColumn("b", "B", width=80)]
    # Column 1 (not the default active cell) so we observe only the gridline.
    on = DataGrid(columns=cols, rows=[{"a": 1, "b": 2}], page=None, vertical_lines=True)
    assert on._make_cell(0, 1, on._columns[1], on._rows[0]).border is not None
    off = DataGrid(columns=cols, rows=[{"a": 1, "b": 2}], page=None, vertical_lines=False)
    assert off._make_cell(0, 1, off._columns[1], off._rows[0]).border is None
