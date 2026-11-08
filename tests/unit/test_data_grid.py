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


def test_column_headers_use_data_type_accents_without_tinting_neutral_cells():
    g = make_grid()
    header_cells = g._header.content.controls
    assert header_cells[0].content.color == Colors.TEXT_MUTED_DARK
    assert header_cells[2].content.color == Colors.PRIMARY
    assert g._cell_boxes[(0, 2)].content.color == Colors.TEXT_PRIMARY


def test_explicit_and_signed_column_roles_color_plain_cell_values():
    columns = [
        GridColumn("profit", "الربح", kind="money", color_role="signed"),
        GridColumn("expense", "المصروف", kind="money", color_role="danger"),
        GridColumn("conditional", "المستحق", kind="money", color_role=lambda value, row: "warning" if value else None),
    ]
    g = DataGrid(
        columns=columns,
        rows=[
            {"profit": 12, "expense": 4, "conditional": 2},
            {"profit": -3, "expense": 0, "conditional": 0},
        ],
    )
    assert g._cell_boxes[(0, 0)].content.color == Colors.SUCCESS_DARKER
    assert g._cell_boxes[(1, 0)].content.color == Colors.DANGER_DARKER
    assert g._cell_boxes[(0, 1)].content.color == Colors.DANGER_DARKER
    assert g._cell_boxes[(1, 2)].content.color == Colors.TEXT_PRIMARY


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


def test_tab_steps_to_next_column_including_read_only():
    g = make_grid()
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("Tab"))
    assert g.nav.cell == (0, 1)  # read-only category is not skipped


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
    g._handle_key(FakeKey("F2"))          # open editor
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
    g._handle_key(FakeKey("F2"))           # open editor
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
    g._handle_key(FakeKey("F2"))
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
    g._handle_key(FakeKey("F2"))

    assert g._picker_control is not None      # inline picker panel is open
    assert [v for v, _ in g._picker_visible] == [7, 9]

    g._picker_choose(1)                        # choose the second option (9)

    assert g._picker_control is None
    assert g.row_at(0)["item"] == 9
    assert calls == [(0, "item", 9)]
    assert isinstance(g._cell_boxes[(0, 0)].content, ft.Text)
    assert g._cell_boxes[(0, 0)].content.value == "9"


def test_picker_arrow_navigation_keeps_the_filtered_list():
    g = DataGrid(
        columns=PICK_COLS,
        rows=[{"item": None, "qty": 1}],
        page=None,
        picker_options=lambda col, _row: [(7, "مادة سبعة"), (9, "مادة تسعة")] if col.key == "item" else [],
    )
    g.arm()
    g._handle_key(FakeKey("F2"))
    before = list(g._picker_visible)
    g._handle_key(FakeKey("Arrow Down"))
    assert g._picker_visible == before        # list is NOT reset
    assert g._picker_highlight == 1


def test_picker_escape_closes_without_changing_the_value():
    g = DataGrid(
        columns=PICK_COLS,
        rows=[{"item": 7, "qty": 1}],
        page=None,
        picker_options=lambda col, _row: [(7, "مادة سبعة"), (9, "مادة تسعة")] if col.key == "item" else [],
    )

    g.arm()
    g._handle_key(FakeKey("F2"))
    assert g._picker_control is not None

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


def test_enter_commits_and_advances_to_next_cell():
    g = make_grid()
    g._on_cell_click(0, 0)           # name (first editable)
    g._handle_key(FakeKey("F2"))     # open editor (keeps value)
    assert g._edit_field is not None
    g._edit_field.value = "دفتر"
    g._handle_key(FakeKey("Enter"))  # commit + advance
    assert g._rows[0]["name"] == "دفتر"
    assert g.nav.cell == (0, 1)      # next column (read-only included)


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


def test_tab_at_last_cell_appends_a_row():
    g = make_grid()
    last = len(ROWS) - 1
    g._on_cell_click(last, len(COLS) - 1)
    g._handle_key(FakeKey("Tab"))
    assert len(g.rows) == len(ROWS) + 1


def test_shift_tab_steps_backwards():
    g = make_grid()
    g._on_cell_click(1, 2)
    g._handle_key(FakeKey("Tab", shift=True))
    assert g.nav.cell == (1, 1)


def test_tab_while_editing_commits_and_advances():
    g = make_grid()
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("F2"))
    assert g._edit_field is not None
    g._edit_field.value = "قلم جاف"
    g._handle_key(FakeKey("Tab"))
    assert g._edit_field is None
    assert g._rows[0]["name"] == "قلم جاف"
    assert g.nav.cell == (0, 1)


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


def test_enter_skips_picker_cell_and_moves_on():
    g = DataGrid(
        columns=PICK_COLS,
        rows=[{"item": None, "qty": 1}],
        page=None,
        picker_options=lambda col, _row: [(7, "مادة سبعة")] if col.key == "item" else [],
    )
    g.arm()  # nav at (0, 0) = the picker column
    g._handle_key(FakeKey("Enter"))
    assert g._picker_control is None   # Enter never opens the picker
    assert g.nav.cell == (0, 1)        # moves on to the next editable cell


def test_type_to_edit_maps_latin_first_key_to_arabic():
    g = make_grid()
    g._on_cell_click(0, 0)           # name (text)
    g._handle_key(FakeKey("a"))      # latin 'a' -> standard Arabic 'ش'
    assert g._edit_field is not None
    assert g._edit_field.value == "ش"


def test_escape_delegates_to_shell_home_hook_when_not_consumed():
    calls = []
    page = SimpleNamespace(_nano_shell_escape=lambda: calls.append(True))
    g = DataGrid(columns=COLS, rows=ROWS, page=page)
    g._handle_key(FakeKey("Escape"))
    assert calls == [True]


def test_escape_clears_selection_before_delegating():
    calls = []
    page = SimpleNamespace(_nano_shell_escape=lambda: calls.append(True))
    g = DataGrid(columns=COLS, rows=ROWS, page=page)
    g._checked.add(1)
    g._handle_key(FakeKey("Escape"))
    assert calls == []
    assert not g._checked


def test_single_enter_does_not_double_advance():
    # The editor's on_submit *and* the page keyboard handler both fire for one
    # physical Enter; the duplicate must not skip the next column.
    g = make_grid()
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("F2"))
    g._edit_field.value = "دفتر"
    g._edit_field.on_submit(None)     # editor submit
    g._handle_key(FakeKey("Enter"))   # page handler, same physical press
    assert g._rows[0]["name"] == "دفتر"
    assert g.nav.cell == (0, 1)


def test_enter_advances_once_after_guard_window():
    # Deterministic: don't depend on wall-clock sleep. A commit arms the
    # duplicate-press guard; once that window has elapsed (timestamp in the
    # past) the next Enter must advance normally.
    g = make_grid()
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("F2"))
    g._edit_field.value = "دفتر"
    g._edit_field.on_submit(None)   # commit -> (0, 1), arms the guard
    g._last_commit_time = 0.0       # simulate the guard window expiring
    g._handle_key(FakeKey("Enter"))
    assert g.nav.cell == (0, 2)


def test_enter_after_arrow_navigation_continues_from_that_cell():
    # Moving with arrows must disarm the duplicate-press guard, so the next
    # Enter navigates from where the arrows stopped, not from a stale cell.
    g = make_grid()
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("F2"))
    g._edit_field.value = "دفتر"
    g._handle_key(FakeKey("Enter"))       # commit -> (0, 1)
    g._handle_key(FakeKey("Arrow Left"))  # -> (0, 0)
    assert g.nav.cell == (0, 0)
    g._handle_key(FakeKey("Enter"))       # continue from arrow position
    assert g.nav.cell == (0, 1)


class _FakeScrollable:
    def __init__(self):
        self.calls = []
        self.controls = []

    def scroll_to(self, **kwargs):
        self.calls.append(kwargs)

    def update(self):
        pass


def test_arrow_navigation_scrolls_active_row_into_view():
    page = SimpleNamespace(on_keyboard_event=None)
    g = DataGrid(columns=COLS, rows=ROWS, page=page)
    viewport = _FakeScrollable()
    g._viewport = viewport

    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("Arrow Down"))

    assert any(call.get("key") == f"{g._uid}-r1" for call in viewport.calls)


def test_picker_scrolls_panel_and_highlighted_result_into_view():
    page = SimpleNamespace(on_keyboard_event=None)
    cols = [GridColumn("item", "المادة", editable=True, kind="picker")]
    rows = [{"item": 1}, {"item": 2}]
    g = DataGrid(
        columns=cols, rows=rows, page=page,
        picker_options=lambda col, row: [(1, "أ"), (2, "ب")],
    )
    viewport = _FakeScrollable()
    g._viewport = viewport
    g._on_cell_click(0, 0)
    g._begin_edit()  # opens the inline picker

    assert any(call.get("key") == f"{g._uid}-picker" for call in viewport.calls)

    results = _FakeScrollable()
    g._picker_list = results
    g._picker_move(1)
    assert any(call.get("key") == f"{g._uid}-pk1" for call in results.calls)


def test_single_cell_commit_repaints_only_its_row():
    # Perf regression guard: editing one cell must not rebuild the whole body
    # (which made commits O(rows) and the table feel sluggish on long lists).
    g = make_grid()
    calls = {"body": 0, "row": 0}
    orig_body, orig_row = g._render_body, g._render_row

    def counting_body():
        calls["body"] += 1
        orig_body()

    def counting_row(r):
        calls["row"] += 1
        orig_row(r)

    g._render_body = counting_body
    g._render_row = counting_row
    g._apply_user_changes([(0, "name", "قلم جديد")], record_history=False)

    assert calls == {"body": 0, "row": 1}


def test_grouped_grid_commit_falls_back_to_full_rebuild():
    g = make_grid()
    g.set_group_by(lambda row: row.get("category"))
    calls = {"body": 0}
    orig_body = g._render_body

    def counting_body():
        calls["body"] += 1
        orig_body()

    g._render_body = counting_body
    g._apply_user_changes([(0, "name", "قلم جديد")], record_history=False)
    assert calls["body"] == 1


def test_large_grid_switches_to_on_demand_building():
    from nano_offline.components.data_grid import _ON_DEMAND_THRESHOLD

    assert make_grid()._body.build_controls_on_demand is False
    n = _ON_DEMAND_THRESHOLD + 1
    big = DataGrid(columns=COLS, rows=[dict(ROWS[0]) for _ in range(n)], page=None)
    assert big._body.build_controls_on_demand is True


def test_large_grid_scrolls_by_offset_not_stale_key():
    from nano_offline.components.data_grid import _ON_DEMAND_THRESHOLD

    page = SimpleNamespace(on_keyboard_event=None)
    n = _ON_DEMAND_THRESHOLD + 20
    big = DataGrid(columns=COLS, rows=[dict(ROWS[0]) for _ in range(n)], page=page)
    viewport = _FakeScrollable()
    big._viewport = viewport

    big._scroll_row_into_view(n - 1)

    expected = max(0, (n - 1) * big._row_height - 2 * big._row_height)
    assert viewport.calls[-1].get("offset") == expected
    assert "key" not in viewport.calls[-1]


def test_editing_a_cell_fires_the_commit_sound_hook_once():
    g = make_grid()
    calls = []
    g._notify_cell_edited = lambda: calls.append(1)
    g._apply_user_changes([(0, "name", "جديد")], record_history=False)
    assert calls == [1]


def test_picker_commit_does_not_fire_the_generic_cell_sound():
    page = SimpleNamespace(on_keyboard_event=None)
    cols = [GridColumn("item", "المادة", editable=True, kind="picker")]
    rows = [{"item": 1}, {"item": 2}]
    g = DataGrid(columns=cols, rows=rows, page=page, picker_options=lambda c, r: [(1, "أ"), (2, "ب")])
    calls = []
    g._notify_cell_edited = lambda: calls.append(1)
    g._on_cell_click(0, 0)
    g._begin_edit()
    g._picker_choose(1)
    assert calls == []


def test_typing_on_a_picker_cell_prefills_the_search():
    page = SimpleNamespace(on_keyboard_event=None)
    cols = [GridColumn("item", "المادة", editable=True, kind="picker")]
    rows = [{"item": 1}, {"item": 2}]
    g = DataGrid(
        columns=cols, rows=rows, page=page,
        picker_options=lambda c, r: [(1, "رز"), (2, "خبز")],
    )
    g._on_cell_click(0, 0)
    g._handle_key(FakeKey("ر"))  # first typed char starts the picker search

    assert g._picker_search is not None and g._picker_search.value == "ر"
    assert [label for _value, label in g._picker_visible] == ["رز"]


def test_rows_property_returns_copies_but_set_cell_persists_non_column_keys():
    # Pitfall that caused the item-edit bug: mutating ``grid.rows[i]`` mutates a
    # throwaway copy; use set_cell for keys that are not columns (e.g. ``_id``).
    g = make_grid()
    g.rows[0]["_id"] = 999
    assert "_id" not in g._rows[0]
    g.set_cell(0, "_id", 42, notify=False)
    assert g._rows[0]["_id"] == 42


def test_set_cell_persists_values_for_hidden_columns():
    # add_row only keeps visible columns; set_cell must still store a value for
    # a hidden column (used to prefill every field when editing an item).
    g = DataGrid(columns=COLS, rows=[], page=None, hidden_keys={"category"})
    idx = g.add_row()
    g.set_cell(idx, "category", "قرطاسية", notify=False)
    assert g._rows[idx]["category"] == "قرطاسية"
