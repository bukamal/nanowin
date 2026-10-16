"""Unit tests for the DataGrid control's key wiring and cell bookkeeping.
The grid builds headless (no running Flet app), so we can drive its key
handler with a tiny fake KeyboardEvent and assert on the pure state it
exposes -- no rendering required.
"""
from __future__ import annotations

from nano_offline.components.data_grid import DataGrid
from nano_offline.components.grid_model import GridColumn
from nano_offline.core.theme import Colors


class FakeKey:
    def __init__(self, key, *, shift=False, ctrl=False):
        self.key = key
        self.shift = shift
        self.ctrl = ctrl
        self.alt = False
        self.meta = False


COLS = [
    GridColumn("name", "الاسم", editable=True),
    GridColumn("category", "التصنيف", width=120),
    GridColumn("price", "السعر", width=100, kind="money", editable=True),
]

ROWS = [
    {"id": 1, "name": "قلم", "category": "قرطاسية", "price": 1.5},
    {"id": 2, "name": "ورق", "category": "قرطاسية", "price": 10.0},
    {"id": 3, "name": "حبر", "category": "", "price": 0.0},
]


def make_grid(rows=None, **kwargs):
    return DataGrid(columns=COLS, rows=ROWS if rows is None else rows, page=None, **kwargs)


def test_cell_boxes_cover_all_rows_and_columns():
    g = make_grid()
    assert len(g._cell_boxes) == len(ROWS) * len(COLS)
    assert g.row_at(1)["name"] == "ورق"
    assert g.row_at(9) is None


def test_first_cell_is_highlighted_on_build():
    g = make_grid()
    assert g._cell_boxes[(0, 0)].bgcolor == Colors.PRIMARY_BG
    assert g._cell_boxes[(1, 0)].bgcolor is None


def test_unarmed_grid_ignores_navigation():
    g = make_grid()
    g._handle_key(FakeKey("Arrow Down"))
    assert g.nav.cell == (0, 0)  # never armed


def test_arming_via_click_enables_keyboard_navigation():
    g = make_grid()
    g._on_cell_click(1, 1)  # arm + select
    assert g._armed is True
    assert g.nav.cell == (1, 1)
    g._handle_key(FakeKey("Arrow Down"))
    assert g.nav.cell == (2, 1)
    # highlight followed the cursor
    assert g._cell_boxes[(2, 1)].bgcolor == Colors.PRIMARY_BG
    assert g._cell_boxes[(1, 1)].bgcolor is None


def test_tab_hops_only_editable_columns():
    g = make_grid()
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("Tab"))
    assert g.nav.cell == (0, 2)  # skips read-only category


def test_delete_clears_editable_cell_and_notifies():
    calls = []
    g = make_grid(on_cell_commit=lambda r, k, v: calls.append((r, k, v)))
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("Delete"))
    assert g._rows[0]["name"] == ""
    assert calls == [(0, "name", "")]


def test_commit_reverts_display_when_callback_raises():
    def boom(r, k, v):
        raise ValueError("duplicate")

    g = make_grid(on_cell_commit=boom)
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("Enter"))       # open editor
    assert g._edit_field is not None
    g._edit_field.value = "قلم جديد"
    g._handle_key(FakeKey("Enter"))       # commit -> callback raises
    assert g._edit_field is None
    # optimistic value was rolled back to what the model still holds
    assert g._rows[0]["name"] == "قلم"


def test_row_action_column_added_only_with_activate():
    plain = make_grid()
    with_action = make_grid(on_row_activate=lambda row: None)
    assert len(plain._row_boxes[0].content.controls) == len(COLS)
    assert len(with_action._row_boxes[0].content.controls) == len(COLS) + 1


def test_set_rows_resizes_and_clamps_cursor():
    g = make_grid()
    g._on_cell_click(2, 2)
    g.set_rows([ROWS[0]])
    assert len(g._rows) == 1
    assert g.nav.cell == (0, 2)  # row clamped, column kept


def test_empty_grid_renders_placeholder():
    g = make_grid(rows=[])
    assert g._cell_boxes == {}
    assert g.row_at(0) is None


def test_add_row_appends_blank_and_selects_first_editable():
    g = make_grid()
    index = g.add_row()
    assert index == len(ROWS)
    blank = g.row_at(index)
    assert all(blank[col.key] == "" for col in COLS)
    # cursor lands on the first editable cell of the new row, ready to type
    assert g.nav.cell == (index, 0)
    assert g._cell_boxes[(index, 0)].bgcolor == Colors.PRIMARY_BG
    assert g._armed is True


def test_add_row_seeds_given_values_and_ignores_extra_keys():
    g = make_grid()
    g.add_row({"name": "مسطرة", "unknown": 1})
    row = g.row_at(len(ROWS))
    assert row["name"] == "مسطرة"
    assert "unknown" not in row


def test_arm_enables_navigation_without_a_click():
    g = make_grid()
    g.arm()
    g._handle_key(FakeKey("Arrow Down"))
    assert g.nav.cell == (1, 0)


def test_rows_property_returns_a_copy():
    g = make_grid()
    snapshot = g.rows
    snapshot[0]["name"] = "mutated"
    assert g.row_at(0)["name"] == "قلم"


def test_remove_row_drops_and_rerenders():
    g = make_grid()
    g.remove_row(0)
    assert len(g._rows) == len(ROWS) - 1
    assert g.row_at(0)["name"] == "ورق"
    g.remove_row(99)  # out of range: no-op
    assert len(g._rows) == len(ROWS) - 1
