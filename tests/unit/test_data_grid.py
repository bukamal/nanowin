"""Unit tests for the DataGrid control's key wiring and cell bookkeeping.
The grid builds headless (no running Flet app), so we can drive its key
handler with a tiny fake KeyboardEvent and assert on the pure state it
exposes -- no rendering required.
"""
from __future__ import annotations

from types import SimpleNamespace

import flet as ft

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


def test_disarmed_grid_does_not_steal_search_or_new_shortcuts():
    calls = []
    g = SimpleNamespace(
        _picker_control=None,
        _edit_field=None,
        _shortcuts_enabled=False,
        _on_search_shortcut=lambda: calls.append("search"),
        _on_new_shortcut=lambda: calls.append("new"),
        _checked=set(),
        _range_anchor=None,
        _armed=False,
    )

    DataGrid._handle_key(g, FakeKey("/"))
    DataGrid._handle_key(g, FakeKey("n", ctrl=True))
    assert calls == []

    g._shortcuts_enabled = True
    DataGrid._handle_key(g, FakeKey("/"))
    DataGrid._handle_key(g, FakeKey("n", ctrl=True))
    assert calls == ["search", "new"]


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


def test_edit_formatter_shows_display_value_and_parser_restores_storage():
    g = make_grid(
        edit_formatter=lambda col, value, row: f"{float(value) * 10:.0f}" if col.kind == "money" else None,
        parser=lambda col, text: float(text) / 10 if col.kind == "money" else text,
    )
    g._on_cell_click(0, 2)                 # price cell of the first row
    g._handle_key(FakeKey("Enter"))        # open editor
    assert g._edit_field is not None
    assert g._edit_field.value == "15"     # 1.5 rendered in the display currency
    g._edit_field.value = "200"
    g._handle_key(FakeKey("Enter"))        # commit
    assert g._edit_field is None
    assert g._rows[0]["price"] == 20.0     # parsed back to the stored value


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


def test_shift_arrows_select_a_range_and_copy_it_as_tsv():
    class Clipboard:
        value = ""

        def set_clipboard(self, value):
            self.value = value

    clipboard = Clipboard()
    g = DataGrid(columns=COLS, rows=ROWS, page=clipboard)
    g.arm()
    g._handle_key(FakeKey("Arrow Right", shift=True))
    g._handle_key(FakeKey("Arrow Down", shift=True))

    assert g._selection_cells() == {(0, 0), (0, 1), (1, 0), (1, 1)}
    assert g._cell_boxes[(0, 1)].bgcolor == Colors.PRIMARY_BG
    g._copy_selection()
    assert clipboard.value == "قلم\tقرطاسية\nورق\tقرطاسية"


def test_tsv_paste_is_one_undoable_redoable_operation():
    calls = []
    g = make_grid(on_cell_commit=lambda row, key, value: calls.append((row, key, value)))

    assert g._paste_matrix([["مبراة", "غير قابل للتحرير", "2.5"], ["كتاب", "غير قابل للتحرير", "3"]])
    assert (g.row_at(0)["name"], g.row_at(0)["price"]) == ("مبراة", 2.5)
    assert (g.row_at(1)["name"], g.row_at(1)["price"]) == ("كتاب", 3.0)

    assert g._replay_history(redo=False)
    assert (g.row_at(0)["name"], g.row_at(0)["price"]) == ("قلم", 1.5)
    assert (g.row_at(1)["name"], g.row_at(1)["price"]) == ("ورق", 10.0)

    assert g._replay_history(redo=True)
    assert (g.row_at(0)["name"], g.row_at(0)["price"]) == ("مبراة", 2.5)
    assert (g.row_at(1)["name"], g.row_at(1)["price"]) == ("كتاب", 3.0)
    assert calls


def test_clipboard_shortcuts_paste_undo_and_redo():
    class Clipboard:
        value = "قلم جديد\t\t4"

        def get_clipboard(self):
            return self.value

    clipboard = Clipboard()
    g = DataGrid(columns=COLS, rows=ROWS, page=clipboard)
    g.arm()

    g._handle_key(FakeKey("v", ctrl=True))
    assert g.row_at(0)["name"] == "قلم جديد"
    assert g.row_at(0)["price"] == 4.0

    g._handle_key(FakeKey("z", ctrl=True))
    assert g.row_at(0)["name"] == "قلم"
    assert g.row_at(0)["price"] == 1.5

    g._handle_key(FakeKey("z", ctrl=True, shift=True))
    assert g.row_at(0)["name"] == "قلم جديد"
    assert g.row_at(0)["price"] == 4.0


def test_tsv_paste_validates_whole_range_before_applying():
    errors = []
    calls = []
    g = make_grid(
        on_cell_commit=lambda row, key, value: calls.append((row, key, value)),
        on_edit_error=errors.append,
    )

    assert not g._paste_matrix([["مبراة", "", "2.5"], ["كتاب", "", "ليس رقمًا"]])
    assert g.row_at(0)["name"] == "قلم"
    assert g.row_at(0)["price"] == 1.5
    assert calls == []
    assert errors and "الصف 2" in errors[0]


def test_pasting_picker_values_resolves_labels_using_prior_cells_in_same_row():
    cols = [
        GridColumn("item", "المادة", editable=True, kind="picker"),
        GridColumn("unit", "الوحدة", editable=True, kind="picker"),
    ]

    def options(col, row):
        if col.key == "item":
            return [(1, "عنصر · 123")]
        if row.get("item") == 1:
            return [(12, "قطعة × 1")]
        return []

    g = DataGrid(columns=cols, rows=[{"item": None, "unit": None}], page=None, picker_options=options)

    assert g._paste_matrix([["عنصر", "قطعة × 1"]])
    assert g.row_at(0) == {"item": 1, "unit": 12}


def test_delete_clears_editable_cells_in_selected_range_only():
    g = make_grid()
    g.arm()
    g._handle_key(FakeKey("Arrow Right", shift=True))
    g._handle_key(FakeKey("Arrow Right", shift=True))
    g._handle_key(FakeKey("Arrow Down", shift=True))

    g._handle_key(FakeKey("Delete"))

    assert g.row_at(0)["name"] == ""
    assert g.row_at(0)["category"] == "قرطاسية"
    assert g.row_at(0)["price"] == 0.0
    assert g.row_at(1)["name"] == ""
    assert g.row_at(1)["category"] == "قرطاسية"
    assert g.row_at(1)["price"] == 0.0


def test_undo_restores_dependent_cells_changed_by_commit_callback():
    def commit(row, key, value):
        if key == "item" and value:
            g.set_cell(row, "qty", 2, notify=False)

    g = DataGrid(columns=PICK_COLS, rows=[{"item": None, "qty": 1}], page=None, on_cell_commit=commit)
    assert g._apply_user_changes([(0, "item", 7)], record_history=True)
    assert g.row_at(0) == {"item": 7, "qty": 2}

    assert g._replay_history(redo=False)
    assert g.row_at(0) == {"item": None, "qty": 1}


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


PICK_COLS = [
    GridColumn("item", "المادة", editable=True, kind="picker"),
    GridColumn("qty", "الكمية", width=90, align="end", kind="number", editable=True),
]


def test_picker_cell_delegates_to_on_pick_without_opening_an_editor():
    calls = []
    g = DataGrid(columns=PICK_COLS, rows=[{"item": None, "qty": 1}], page=None,
                 on_pick=lambda r, col: calls.append((r, col.key)))
    g.arm()
    g._handle_key(FakeKey("Enter"))
    assert calls == [(0, "item")]
    assert g._edit_field is None  # picker never materialises a TextField


def test_picker_options_edit_inside_active_cell_and_commit_original_value():
    calls = []
    g = DataGrid(
        columns=PICK_COLS,
        rows=[{"item": None, "qty": 1}],
        page=None,
        picker_options=lambda col, _row: [(7, "مادة سبعة"), (9, "مادة تسعة")] if col.key == "item" else [],
        on_cell_commit=lambda row, key, value: calls.append((row, key, value)),
    )

    g.arm()
    g._handle_key(FakeKey("Enter"))

    editor = g._cell_boxes[(0, 0)].content
    assert isinstance(editor, ft.Dropdown)
    assert g._picker_control is editor

    editor.value = "1"
    editor.on_change(SimpleNamespace(control=editor))

    assert g._picker_control is None
    assert g.row_at(0)["item"] == 9
    assert calls == [(0, "item", 9)]
    assert isinstance(g._cell_boxes[(0, 0)].content, ft.Text)
    assert g._cell_boxes[(0, 0)].content.value == "9"


def test_picker_ignores_unmatched_search_text_and_escape_restores_cell():
    g = DataGrid(
        columns=PICK_COLS,
        rows=[{"item": 7, "qty": 1}],
        page=None,
        picker_options=lambda col, _row: [(7, "مادة سبعة"), (9, "مادة تسعة")] if col.key == "item" else [],
    )

    g.arm()
    g._handle_key(FakeKey("Enter"))
    editor = g._picker_control
    editor.value = "بحث غير مطابق"
    editor.on_change(SimpleNamespace(control=editor))

    assert g._picker_control is editor
    assert g.row_at(0)["item"] == 7

    g._handle_key(FakeKey("Escape"))

    assert g._picker_control is None
    assert g.row_at(0)["item"] == 7
    assert isinstance(g._cell_boxes[(0, 0)].content, ft.Text)
    assert g._cell_boxes[(0, 0)].content.value == "7"


def test_set_cell_writes_and_notifies_then_reverts_on_failure():
    calls = []

    def boom(index, key, value):
        raise ValueError("nope")

    ok = DataGrid(columns=PICK_COLS, rows=[{"item": None, "qty": 1}], page=None,
                  on_cell_commit=lambda i, k, v: calls.append((i, k, v)))
    ok.set_cell(0, "item", 7)
    assert ok.row_at(0)["item"] == 7
    assert calls == [(0, "item", 7)]

    bad = DataGrid(columns=PICK_COLS, rows=[{"item": None, "qty": 1}], page=None,
                   on_cell_commit=boom)
    bad.set_cell(0, "item", 9)
    assert bad.row_at(0)["item"] is None  # rolled back


def test_zebra_stripes_alternate_and_can_be_disabled():
    striped = make_grid()
    assert striped._row_bg_for(0) == Colors.WHITE
    assert striped._row_bg_for(1) == Colors.BACKGROUND_ALT
    plain = make_grid(striped=False)
    assert plain._row_bg_for(1) == Colors.WHITE


def test_footer_row_is_pinned_outside_the_scrolling_body_and_removed():
    g = make_grid()
    assert g._footer_slot.visible is False
    assert len(g._body.controls) == len(ROWS)
    g.set_footer({"name": "الإجمالي", "price": "11.5"})
    assert g._footer_slot.visible is True
    assert g._footer_slot.content is not None
    # Pinned: the footer must not be part of the scrollable row list.
    assert len(g._body.controls) == len(ROWS)
    g.set_footer(None)
    assert g._footer_slot.visible is False
    assert g._footer_slot.content is None


def test_type_to_edit_starts_editor_with_the_typed_character():
    g = make_grid()
    g._on_cell_click(0, 0)           # arm the first editable cell (name)
    g._handle_key(FakeKey("ق"))      # a printable key starts editing
    assert g._edit_field is not None
    assert g._edit_field.value == "ق"
    g._edit_field.value = "قلم رصاص"
    g._handle_key(FakeKey("Enter"))  # commit
    assert g._rows[0]["name"] == "قلم رصاص"


def test_enter_commits_and_advances_to_next_editable_cell():
    g = make_grid()
    g._on_cell_click(0, 0)           # name (first editable)
    g._handle_key(FakeKey("Enter"))  # open editor (keeps value)
    assert g._edit_field is not None
    g._edit_field.value = "دفتر"
    g._handle_key(FakeKey("Enter"))  # commit + advance
    assert g._rows[0]["name"] == "دفتر"
    assert g.nav.cell == (0, 2)      # next editable cell (price)


def test_enter_wraps_to_next_row():
    g = make_grid()
    g._on_cell_click(0, 2)           # price (last editable of row 0)
    g._handle_key(FakeKey("F2"))     # open editor keeping value
    assert g._edit_field is not None
    g._edit_field.value = "9"
    g._handle_key(FakeKey("Enter"))
    assert g.nav.cell == (1, 0)      # wrapped to first editable of row 1


def test_enter_at_last_cell_appends_a_row():
    g = make_grid()
    last = len(ROWS) - 1
    g._on_cell_click(last, 2)        # last editable cell of the grid
    g._handle_key(FakeKey("F2"))
    g._edit_field.value = "1"
    g._handle_key(FakeKey("Enter"))
    assert len(g.rows) == len(ROWS) + 1


def test_ctrl_a_selects_the_whole_grid():
    g = make_grid()
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("a", ctrl=True))
    assert g._range_anchor == (0, 0)
    assert g.nav.cell == (len(ROWS) - 1, len(COLS) - 1)


def test_ctrl_home_and_end_jump_to_corners():
    g = make_grid()
    g._on_cell_click(1, 1)
    g._handle_key(FakeKey("End", ctrl=True))
    assert g.nav.cell == (len(ROWS) - 1, len(COLS) - 1)
    g._handle_key(FakeKey("Home", ctrl=True))
    assert g.nav.cell == (0, 0)
