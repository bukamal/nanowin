"""Unit tests for the pure-Python DataGrid navigation model."""
from __future__ import annotations

from nano_offline.components.grid_model import GridColumn, GridNavigator


def make_nav(columns, row_count=3):
    return GridNavigator(columns, row_count=row_count)


NAME = GridColumn("name", "الاسم", editable=True)
CAT = GridColumn("category", "التصنيف")
QTY = GridColumn("quantity", "الكمية", kind="number", editable=True)
PRICE = GridColumn("selling_price", "السعر", kind="money", editable=True)

COLS = [NAME, CAT, QTY, PRICE]


def test_construction_defaults_and_clamp():
    nav = make_nav(COLS, row_count=-5)
    assert nav.row == 0 and nav.col == 0
    assert nav.row_count == 0
    assert nav.has_rows is False
    assert nav.editing is False


def test_has_rows_requires_columns_and_rows():
    assert make_nav(COLS, 0).has_rows is False
    assert make_nav([], 5).has_rows is False
    assert make_nav(COLS, 1).has_rows is True


def test_column_and_key_and_editable():
    nav = make_nav(COLS)
    assert nav.key == "name"
    assert nav.is_editable is True
    nav.select(0, 1)  # category: read-only
    assert nav.key == "category"
    assert nav.is_editable is False


def test_empty_columns_column_is_none():
    nav = make_nav([])
    assert nav.column is None
    assert nav.key is None
    assert nav.is_editable is False


def test_move_bounds_do_not_escape_grid():
    nav = make_nav(COLS, row_count=2)
    assert nav.move_up() is False and nav.row == 0
    assert nav.move_left() is False and nav.col == 0
    nav.last()
    assert nav.row == 1
    assert nav.move_down() is False and nav.row == 1
    nav.end()
    assert nav.col == len(COLS) - 1
    assert nav.move_right() is False and nav.col == len(COLS) - 1


def test_move_returns_changed_flag():
    nav = make_nav(COLS, row_count=2)
    assert nav.move_down() is True
    assert nav.move_down() is False  # already at the last row
    assert nav.move_right() is True
    assert nav.move_up() is True
    assert nav.cell == (0, 1)


def test_home_end_first_last():
    nav = make_nav(COLS, row_count=4)
    nav.select(2, 2)
    assert nav.home() is True and nav.col == 0
    assert nav.home() is False  # no-op
    assert nav.end() is True and nav.col == len(COLS) - 1
    assert nav.first() is True and nav.row == 0
    assert nav.last() is True and nav.row == 3


def test_page_moves_by_page_size():
    nav = make_nav(COLS, row_count=25)
    nav.page(1, 10)
    assert nav.row == 10
    nav.page(1, 10)
    assert nav.row == 20
    nav.page(1, 10)
    assert nav.row == 24  # clamped
    nav.page(-1, 10)
    assert nav.row == 14


def test_page_size_minimum_one():
    nav = make_nav(COLS, row_count=5)
    nav.page(1, 0)
    assert nav.row == 1


# ------------------------------------------------------------------ Tab order
def test_tab_skips_read_only_columns():
    nav = make_nav(COLS, row_count=2)
    nav.select(0, 0)  # editable: name
    assert nav.tab() is True
    assert nav.cell == (0, 2)  # skips read-only category -> quantity
    assert nav.tab() is True
    assert nav.cell == (0, 3)  # selling_price


def test_tab_wraps_into_next_row():
    nav = make_nav(COLS, row_count=2)
    nav.select(0, 3)  # last editable of row 0
    assert nav.tab() is True
    assert nav.cell == (1, 0)  # first editable of row 1


def test_tab_stops_at_last_editable_cell():
    nav = make_nav(COLS, row_count=2)
    nav.select(1, 3)
    assert nav.tab() is False
    assert nav.cell == (1, 3)


def test_shift_tab_steps_backwards_and_stops_at_start():
    nav = make_nav(COLS, row_count=2)
    nav.select(1, 0)
    assert nav.tab(shift=True) is True
    assert nav.cell == (0, 3)
    nav.select(0, 0)
    assert nav.tab(shift=True) is False
    assert nav.cell == (0, 0)


def test_tab_with_no_editable_columns_is_noop():
    cols = [GridColumn("a", "A"), GridColumn("b", "B")]
    nav = make_nav(cols, row_count=3)
    assert nav.tab() is False
    assert nav.cell == (0, 0)


def test_tab_with_no_rows_is_noop():
    nav = make_nav(COLS, row_count=0)
    assert nav.tab() is False


def test_tab_starts_from_non_editable_position():
    nav = make_nav(COLS, row_count=2)
    nav.select(0, 1)  # read-only category
    assert nav.tab() is True
    assert nav.cell == (0, 2)


# ------------------------------------------------------------------- editing
def test_enter_opens_then_commits():
    nav = make_nav(COLS)
    nav.select(0, 0)
    assert nav.enter() == "edit"
    assert nav.editing is True
    assert nav.enter() == "commit"
    assert nav.editing is False


def test_enter_on_read_only_is_none():
    nav = make_nav(COLS)
    nav.select(0, 1)
    assert nav.enter() == "none"
    assert nav.editing is False


def test_begin_edit_refused_on_read_only():
    nav = make_nav(COLS)
    nav.select(0, 1)
    assert nav.begin_edit() is False
    assert nav.editing is False


def test_cancel_reports_and_clears_editing():
    nav = make_nav(COLS)
    nav.begin_edit()
    assert nav.cancel() is True
    assert nav.cancel() is False


def test_move_cancels_editing():
    nav = make_nav(COLS, row_count=2)
    nav.begin_edit()
    assert nav.editing is True
    nav.move_down()
    assert nav.editing is False


def test_select_and_set_rows_clear_editing_and_clamp():
    nav = make_nav(COLS, row_count=5)
    nav.select(4, 3)
    nav.begin_edit()
    nav.set_rows(2)
    assert nav.row == 1 and nav.editing is False
    nav.select(99, 99)
    assert nav.cell == (1, len(COLS) - 1)


def test_negative_index_select_clamps_to_zero():
    nav = make_nav(COLS, row_count=3)
    nav.select(-2, -1)
    assert nav.cell == (0, 0)
