"""Editable, keyboard-driven data grid for the desktop layout.

Renders like a modern spreadsheet: a rounded, clipped card holding a
pinned, tinted header row, subtly zebra-striped body rows, a hover
highlight, a soft-ringed active cell, and inline editing opened with
Enter (or a second click) on an editable column. Navigation semantics
live in the framework-free :mod:`nano_offline.components.grid_model`;
this module is only the Flet rendering + key wiring around it.

Two presentation switches exist for callers: ``boxed`` (draw the table's
own rounded border + shadow -- used by the full-screen Materials grid, and
left off where a caller already supplies its own frame, e.g. the bulk
sheets) and ``center_cells`` (centre every header and cell regardless of a
column's own ``align``).

Why the "selection highlight" model instead of making every cell a
``TextField``: a grid of live text inputs needs focus juggling on every
arrow key and swallows keystrokes in ways that are hard to reason about.
Here only the *one* cell being edited owns a ``TextField`` -- arrows move
a highlight (no focus needed), and Enter materialises an input just for
that cell. Shift+arrows extend a rectangular selection; Ctrl+C/Ctrl+V use
tab-separated display values, and Ctrl+Z/Ctrl+Y replay committed edits.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from typing import Callable

import flet as ft

from nano_offline.core.theme import Colors, Radius, Shadow
from nano_offline.components.grid_model import GridColumn, GridNavigator
from nano_offline.components.status_pill import status_pill


def _norm_key(key: str | None) -> str:
    """Normalise a Flet key string so matching does not depend on whether
    the client reports ``"Arrow Up"`` or ``"ArrowUp"`` / ``"arrowup"``."""
    return (key or "").strip().lower().replace(" ", "").replace("_", "")


_UP, _DOWN, _LEFT, _RIGHT = "arrowup", "arrowdown", "arrowleft", "arrowright"
_PAGE_UP, _PAGE_DOWN = "pageup", "pagedown"
_HISTORY_LIMIT = 100

# Standard Arabic (Windows 101) keyboard: physical latin key -> Arabic letter.
# Used ONLY to seed the first character of a type-to-edit when the runtime
# reports the raw latin key (an Arabic IME delivers the latin key for the
# very first keystroke, so the composed letter would otherwise be wrong).
_AR_KEY_MAP = {
    "q": "ض", "w": "ص", "e": "ث", "r": "ق", "t": "ف", "y": "غ", "u": "ع",
    "i": "ه", "o": "خ", "p": "ح", "[": "ج", "]": "د",
    "a": "ش", "s": "س", "d": "ي", "f": "ب", "g": "ل", "h": "ا", "j": "ت",
    "k": "ن", "l": "م", ";": "ك", "'": "ط",
    "z": "ئ", "x": "ء", "c": "ؤ", "v": "ر", "b": "لا", "n": "ى", "m": "ة",
    ",": "و", ".": "ز", "/": "ظ", "`": "ذ",
}


def _default_text(col: GridColumn, value) -> str:
    if value is None:
        return ""
    if col.kind in ("number", "money"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return str(value)
        if col.kind == "money":
            return f"{num:,.2f}"
        return str(int(num)) if num == int(num) else f"{num:g}"
    return str(value)


def _default_parser(col: GridColumn, text: str):
    """Turn raw input back into a stored value (raises on bad numbers)."""
    if col.kind in ("number", "money"):
        cleaned = (text or "").strip().replace(",", "")
        if not cleaned:
            return 0.0
        value = float(cleaned)
        if not math.isfinite(value):
            raise ValueError("أدخل رقمًا محدودًا")
        return value
    return (text or "").strip()


class DataGrid(ft.Column):
    """Editable table control. See module docstring for the interaction model."""

    def __init__(
        self,
        *,
        columns: list[GridColumn],
        rows: list[dict] | None = None,
        page: ft.Page | None = None,
        on_cell_commit: Callable[[int, str, object], None] | None = None,
        on_row_activate: Callable[[dict], None] | None = None,
        on_pick: Callable[[int, GridColumn], None] | None = None,
        picker_options: Callable[[GridColumn, dict], list[tuple[object, str]]] | None = None,
        picker_searchable: bool = True,
        formatter: Callable[[GridColumn, object, dict], object] | None = None,
        edit_formatter: Callable[[GridColumn, object, dict], object] | None = None,
        parser: Callable[[GridColumn, str], object] | None = None,
        activate_icon: str = ft.Icons.OPEN_IN_NEW_ROUNDED,
        activate_tooltip: str = "فتح",
        row_height: int = 48,
        header_height: int = 44,
        empty_text: str = "لا توجد بيانات",
        striped: bool = True,
        boxed: bool = False,
        center_cells: bool = False,
        on_sort: Callable[[str | None, str | None], None] | None = None,
        selectable: bool = False,
        on_selection_change: Callable[[list[dict]], None] | None = None,
        on_row_menu: Callable[[dict], list] | None = None,
        viewport_height: int | None = None,
        hidden_keys: set[str] | None = None,
        row_bg: Callable[[dict], str | None] | None = None,
        group_by: Callable[[dict], str | None] | None = None,
        on_row_expand: Callable[[dict], ft.Control | None] | None = None,
        on_search_shortcut: Callable[[], None] | None = None,
        on_new_shortcut: Callable[[], None] | None = None,
        on_edit_error: Callable[[str], None] | None = None,
        on_enter: Callable[[], None] | None = None,
        top_row: ft.Control | None = None,
    ):
        self._all_columns = list(columns)
        self._hidden_keys: set[str] = set(hidden_keys or set())
        self._columns = [c for c in self._all_columns if c.key not in self._hidden_keys]
        self._rows: list[dict] = [dict(r) for r in (rows or [])]
        self._page = page
        self._on_cell_commit = on_cell_commit
        self._on_row_activate = on_row_activate
        self._on_pick = on_pick
        self._picker_options = picker_options
        self._picker_searchable = picker_searchable
        self._formatter = formatter
        self._edit_formatter = edit_formatter
        self._parser = parser
        self._activate_icon = activate_icon
        self._activate_tooltip = activate_tooltip
        self._row_height = row_height
        self._header_height = header_height
        self._empty_text = empty_text
        self._striped = bool(striped)
        self._on_sort = on_sort
        self._selectable = bool(selectable)
        self._on_selection_change = on_selection_change
        self._on_row_menu = on_row_menu
        self._row_bg = row_bg
        self._group_by = group_by
        self._on_row_expand = on_row_expand
        self._on_search_shortcut = on_search_shortcut
        self._on_new_shortcut = on_new_shortcut
        self._on_enter = on_enter
        # An optional control pinned as the very first row *inside* the body
        # (below the column headers) -- e.g. an invoice's party selector, so
        # it reads as a row of the table rather than a field above it.
        self._top_row = top_row
        self._on_edit_error = on_edit_error
        self._expanded: set[int] = set()
        self._sort_key: str | None = None
        self._sort_dir: str | None = None
        self._checked: set[int] = set()
        self._check_buttons: list[ft.IconButton] = []
        self._select_all_button: ft.IconButton | None = None
        self._header: ft.Container | None = None
        # ``boxed`` draws the rounded card + subtle shadow around the whole
        # table (used by the full-screen Materials grid). Callers that mount
        # the grid inside their own bordered container (the bulk-entry sheets)
        # leave it off so the two borders never double up.
        self._boxed = bool(boxed)
        # ``center_cells`` centers every column's header *and* cell content,
        # overriding each column's own ``align`` -- the materials table reads
        # far cleaner with a symmetric, evenly-centred grid than with prices
        # pinned to one edge and names to the other.
        self._center_cells = bool(center_cells)
        self._footer_values: dict[str, str] | None = None

        self.nav = GridNavigator(self._columns, len(self._rows))
        self._cell_boxes: dict[tuple[int, int], ft.Container] = {}
        self._row_boxes: list[ft.Container] = []
        self._edit_field: ft.TextField | None = None
        self._picker_control = None  # truthy while a picker dialog is open
        self._picker_values: dict[str, object] = {}
        # Custom picker state: we own the filtered list + highlight so arrow
        # navigation never depends on the Dropdown's internal filtering (which
        # reset the list while moving through it).
        self._picker_items: list[tuple[object, str]] = []
        self._picker_visible: list[tuple[object, str]] = []
        self._picker_highlight = 0
        self._picker_list = None
        self._picker_dialog = None
        self._picker_row: int | None = None
        self._picker_key: str | None = None
        self._prev_cell: tuple[int, int] | None = None
        self._range_anchor: tuple[int, int] | None = None
        self._painted_selection: set[tuple[int, int]] = set()
        self._history: list[list[tuple[int, str, object, object]]] = []
        self._history_cursor = 0
        # Keyboard nav only reacts once the user has actually engaged the
        # grid (clicked a cell); otherwise a page-level handler would also
        # fire while the search box has focus (e.g. Enter there would open a
        # cell editor). The view calls disarm() when its own fields focus.
        self._armed = False
        self._shortcuts_enabled = True
        self._body = ft.Column(spacing=0)
        # Footer (e.g. invoice totals) lives outside the scrolling body so it
        # stays pinned at the bottom of the table instead of drifting with the
        # rows. It shares the header's column geometry, so cells line up.
        self._footer_slot = ft.Container(visible=False)

        # Header and footer stay pinned; only the body scrolls. All three live
        # inside one rounded, clipped card so the tinted header's top corners
        # and the last row's bottom corners follow the table's radius instead
        # of squaring off against it.
        card = ft.Container(
            ft.Column(
                [
                    self._build_header(),
                    ft.Column([self._body], scroll=ft.ScrollMode.AUTO, expand=True),
                    self._footer_slot,
                ],
                spacing=0,
                expand=True,
            ),
            bgcolor=Colors.WHITE,
            border_radius=Radius.MD,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            expand=True,
        )
        if viewport_height:
            # A bounded height turns the inner body scroll into a real
            # viewport, which keeps the header pinned even when the grid
            # sits inside an outer page-level scroll (e.g. the invoice
            # list). Without it, `expand` already bounds the grid.
            card.height = int(viewport_height)
            card.expand = False
        if self._boxed:
            card.border = ft.border.all(1, Colors.BORDER)
            card.shadow = Shadow.SM
        super().__init__(controls=[card], spacing=0, expand=True)
        self._render_body()
        if page is not None:
            self.attach()

    # ------------------------------------------------------------------ keys
    def attach(self) -> None:
        """Make this grid the page's keyboard handler (called on mount)."""
        if self._page is not None:
            self._page.on_keyboard_event = self._handle_key

    def detach(self) -> None:
        if self._page is not None:
            self._page.on_keyboard_event = None

    def _handle_key(self, e: ft.KeyboardEvent) -> None:
        if self._picker_control is not None:
            key = _norm_key(e.key)
            if key in ("escape", "esc"):
                self._picker_close()
            elif key in ("enter", "numpadenter"):
                self._picker_commit()
            elif key in ("arrowdown", "pagedown"):
                self._picker_move(1)
            elif key in ("arrowup", "pageup"):
                self._picker_move(-1)
            return
        if self._edit_field is not None:
            # While a cell editor is open, Enter/Escape resolve it; every
            # other key is the user typing, so leave it to the TextField.
            key = _norm_key(e.key)
            if key == "enter":
                self._commit_edit(move="next")
            elif key in ("escape", "esc"):
                self._cancel_edit()
            return

        key = _norm_key(e.key)
        if getattr(e, "ctrl", False) and key == "k":
            opener = getattr(self._page, "_nano_open_command_palette", None)
            if callable(opener):
                opener()
            return
        # View-level shortcuts stay live until a view control takes focus:
        # "/" jumps to search, Ctrl+N starts the screen's "new" action.
        if self._shortcuts_enabled and key == "/" and self._on_search_shortcut is not None:
            self._on_search_shortcut()
            return
        if self._shortcuts_enabled and getattr(e, "ctrl", False) and key == "n" and self._on_new_shortcut is not None:
            self._on_new_shortcut()
            return
        if key in ("escape", "esc") and self._checked:
            # Works even when the grid was only "checked" (not cell-armed).
            self.clear_selection()
            return
        if key in ("escape", "esc") and self._range_anchor is not None:
            self._range_anchor = None
            self._paint_selection()
            return
        if not self._armed:
            return

        shift = bool(getattr(e, "shift", False))
        if getattr(e, "ctrl", False) and (key == "y" or (key == "z" and shift)):
            self._replay_history(redo=True)
            return
        if getattr(e, "ctrl", False) and key == "z":
            self._replay_history(redo=False)
            return
        if getattr(e, "ctrl", False) and key == "c":
            self._copy_selection()
            return
        if getattr(e, "ctrl", False) and key == "v":
            self._paste_from_clipboard()
            return
        if getattr(e, "ctrl", False) and key == "a":
            # Select the whole grid, Excel-style.
            if self._rows and self._columns:
                self._range_anchor = (0, 0)
                self.nav.select(len(self._rows) - 1, len(self._columns) - 1)
                self._paint_selection()
            return
        if getattr(e, "ctrl", False) and key == "home":
            self._range_anchor = None
            if self._rows:
                self.nav.select(0, 0)
                self._paint_selection()
            return
        if getattr(e, "ctrl", False) and key == "end":
            self._range_anchor = None
            if self._rows and self._columns:
                self.nav.select(len(self._rows) - 1, len(self._columns) - 1)
                self._paint_selection()
            return

        # Excel-style type-to-edit: a printable character starts editing and
        # replaces the cell (undoable). Pickers still open on click/Enter.
        # Prefer a composed "character" when the runtime provides it; otherwise
        # the raw latin key is mapped to its standard-Arabic letter for the
        # first character only (an Arabic IME returns the latin key first).
        ch = getattr(e, "character", None)
        used_character = isinstance(ch, str) and len(ch) == 1
        raw = ch if used_character else (e.key or "")
        if (
            self.nav.is_editable
            and len(raw) == 1
            and raw.isprintable()
            and not getattr(e, "ctrl", False)
            and not getattr(e, "alt", False)
            and not getattr(e, "meta", False)
        ):
            if used_character:
                prefill = raw
            elif raw.isascii():
                prefill = _AR_KEY_MAP.get(raw.lower(), raw)
            else:
                prefill = raw
            self._begin_edit(prefill=prefill)
            return

        moved = False
        if key == _UP:
            moved = self.nav.move_up()
        elif key == _DOWN:
            moved = self.nav.move_down()
        elif key == _LEFT:
            moved = self.nav.move_left()
        elif key == _RIGHT:
            moved = self.nav.move_right()
        elif key == "tab":
            moved = self.nav.tab(shift)
        elif key == "home":
            moved = self.nav.home()
        elif key == "end":
            moved = self.nav.end()
        elif key == _PAGE_DOWN:
            moved = self.nav.page(1, self._page_rows())
        elif key == _PAGE_UP:
            moved = self.nav.page(-1, self._page_rows())
        elif key == "enter":
            if self._on_enter is not None:
                # The callback receives the active column key and returns
                # True to swallow the Enter (e.g. finish/close the sheet);
                # a falsy return falls through to normal navigation.
                try:
                    handled = self._on_enter(self.nav.key)
                except TypeError:
                    handled = self._on_enter()
                if handled:
                    return
            # Enter always navigates to the next editable cell, wrapping to the
            # next row (appending a row at the very end). Pickers open on
            # click/double-click or F2, never by Enter.
            self._advance_forward()
            return
        elif key == "f2":
            # Edit in place keeping the current value (Excel F2).
            self._begin_edit()
            return
        elif key in ("delete", "backspace"):
            self._clear_active()
            return
        else:
            return
        if moved:
            if shift and key in (_UP, _DOWN, _LEFT, _RIGHT, _PAGE_UP, _PAGE_DOWN, "home", "end"):
                if self._range_anchor is None:
                    self._range_anchor = self._prev_cell or self.nav.cell
            else:
                self._range_anchor = None
            self._paint_selection()

    def _page_rows(self) -> int:
        page = self._page
        height = getattr(page, "height", None) if page is not None else None
        if height:
            return max(1, int(height // self._row_height) - 4)
        return 12

    # ------------------------------------------------------------------ build
    def _col_align(self, col: GridColumn):
        if self._center_cells:
            return ft.alignment.center
        return {
            "start": ft.alignment.center_right,  # leading edge in RTL
            "center": ft.alignment.center,
            "end": ft.alignment.center_left,
        }.get(col.align, ft.alignment.center_right)

    def _edit_align(self, col: GridColumn) -> ft.TextAlign:
        """Text alignment for the inline editor, kept in step with the cell
        it replaces so the value never jumps sideways when the box opens."""
        if self._center_cells or col.align == "center":
            return ft.TextAlign.CENTER
        return ft.TextAlign.LEFT if col.align == "end" else ft.TextAlign.RIGHT

    def _cell_kwargs(self, col: GridColumn) -> dict:
        if col.width is None:
            return {"expand": True}
        return {"width": col.width}

    def _trailing_width(self) -> int:
        return (48 if self._on_row_activate is not None else 0) + (40 if self._on_row_menu is not None else 0)

    def _header_alignment(self, col: GridColumn):
        if self._center_cells or col.align == "center":
            return ft.MainAxisAlignment.CENTER
        return ft.MainAxisAlignment.END if col.align == "end" else ft.MainAxisAlignment.START

    def _header_cell(self, col: GridColumn) -> ft.Control:
        label = ft.Text(
            col.label,
            size=12,
            weight=ft.FontWeight.W_700,
            color=Colors.TEXT_MUTED_DARK,
        )
        sortable = self._on_sort is not None and col.sortable
        if not sortable:
            inner: ft.Control = label
        else:
            arrow = None
            if self._sort_key == col.key:
                arrow = ft.Icon(
                    ft.Icons.ARROW_UPWARD if self._sort_dir == "asc" else ft.Icons.ARROW_DOWNWARD,
                    size=13,
                    color=Colors.PRIMARY,
                )
            inner = ft.Row(
                [label] + ([arrow] if arrow is not None else []),
                spacing=4,
                alignment=self._header_alignment(col),
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        return ft.Container(
            inner,
            height=self._header_height,
            alignment=self._col_align(col),
            padding=ft.padding.symmetric(horizontal=12),
            tooltip=(col.tooltip or (f"ترتيب حسب {col.label}" if sortable else None)),
            ink=bool(sortable),
            on_click=(lambda _, c=col: self._header_click(c)) if sortable else None,
            **self._cell_kwargs(col),
        )

    def _header_cells(self) -> list[ft.Control]:
        cells: list[ft.Control] = []
        if self._on_row_expand is not None:
            cells.append(ft.Container(width=32, height=self._header_height))
        if self._selectable:
            self._select_all_button = ft.IconButton(
                icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                icon_size=18,
                icon_color=Colors.TEXT_FAINT,
                tooltip="تحديد الكل",
                on_click=lambda _: self._toggle_all(),
            )
            cells.append(
                ft.Container(
                    self._select_all_button,
                    width=40,
                    height=self._header_height,
                    alignment=ft.alignment.center,
                )
            )
        for col in self._columns:
            cells.append(self._header_cell(col))
        trail = self._trailing_width()
        if trail:
            cells.append(ft.Container(width=trail, height=self._header_height))
        return cells

    def _build_header(self) -> ft.Container:
        self._header = ft.Container(
            ft.Row(self._header_cells(), spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=Colors.BACKGROUND_ALT,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER_STRONG)),
        )
        return self._header

    def _refresh_header(self) -> None:
        header = self._header
        if header is None:
            return
        header.content = ft.Row(self._header_cells(), spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self._safe_update(header)

    def _header_click(self, col: GridColumn) -> None:
        """Cycle a column through ascending -> descending -> unsorted.

        Sorting itself stays in the view: it owns the full (unpaginated)
        result set, so sorting here would only reorder the visible page.
        """
        if self._on_sort is None or not col.sortable:
            return
        if self._sort_key != col.key:
            self._sort_key, self._sort_dir = col.key, "asc"
        elif self._sort_dir == "asc":
            self._sort_dir = "desc"
        else:
            self._sort_key, self._sort_dir = None, None
        self._refresh_header()
        self._on_sort(self._sort_key, self._sort_dir)

    def set_sort(self, key: str | None, direction: str | None = None) -> None:
        """Update the sort indicator without invoking the view callback."""
        self._sort_key = key
        self._sort_dir = direction if key else None
        self._refresh_header()

    def set_group_by(self, hook: Callable[[dict], str | None] | None) -> None:
        """Turn the labelled group banners on/off (e.g. date buckets).

        Views disable grouping while an explicit column sort is active,
        since banners only make sense over a meaningful row order.
        """
        if hook is self._group_by:
            return
        self._group_by = hook
        self._render_body()
        self._safe_update(self._body)

    def set_hidden_keys(self, keys: set[str] | frozenset[str] | list[str]) -> None:
        """Show/hide columns by key (column-visibility menu), preserving rows."""
        valid = {k for k in keys if any(c.key == k for c in self._all_columns)}
        if valid == self._hidden_keys:
            return
        self._hidden_keys = valid
        self._columns = [c for c in self._all_columns if c.key not in self._hidden_keys]
        self.nav = GridNavigator(self._columns, len(self._rows))
        self._range_anchor = None
        self._painted_selection.clear()
        self._prev_cell = None
        self._refresh_header()
        self._render_body()
        self._refresh_footer()
        self._safe_update(self._body)

    @property
    def hidden_keys(self) -> set[str]:
        return set(self._hidden_keys)

    def visible_keys(self) -> list[str]:
        return [c.key for c in self._columns]

    def _cell_text(self, r: int, c: int) -> str:
        col = self._columns[c]
        row = self._rows[r]
        value = row.get(col.key)
        text = ""
        if self._formatter is not None:
            out = self._formatter(col, value, row)
            if isinstance(out, tuple) and out:
                text = str(out[0])
            elif isinstance(out, ft.Control):
                text = ""
            elif out is not None:
                text = str(out)
        if not text:
            text = _default_text(col, value)
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _copy_selection(self) -> None:
        """Copy the selected rectangle as tab-separated display values."""
        if self._page is None or not self._rows or not self._columns:
            return
        selected = self._selection_cells()
        row_start = min(row for row, _ in selected)
        row_end = max(row for row, _ in selected)
        col_start = min(col for _, col in selected)
        col_end = max(col for _, col in selected)
        output = io.StringIO()
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        for row in range(row_start, row_end + 1):
            writer.writerow([self._cell_text(row, col) for col in range(col_start, col_end + 1)])
        try:
            self._page.set_clipboard(output.getvalue().rstrip("\n"))
        except Exception as exc:
            self._report_edit_error(f"تعذر نسخ الخلايا: {exc}")

    def _paste_from_clipboard(self) -> None:
        if self._page is None:
            return
        try:
            text = self._page.get_clipboard()
        except Exception as exc:
            self._report_edit_error(f"تعذرت قراءة الحافظة: {exc}")
            return
        if not text:
            return
        try:
            matrix = list(csv.reader(io.StringIO(text), delimiter="\t"))
        except csv.Error as exc:
            self._report_edit_error(f"تنسيق الحافظة غير صالح: {exc}")
            return
        if matrix and not any(matrix[-1]) and text.endswith(("\n", "\r")):
            matrix.pop()
        self._paste_matrix(matrix)

    def _paste_matrix(self, matrix: list[list[str]]) -> bool:
        if not matrix or not any(matrix):
            return False
        start_row, start_col = self.nav.cell
        if start_row + len(matrix) > len(self._rows) or start_col + max(map(len, matrix), default=0) > len(self._columns):
            self._report_edit_error("النطاق المنسوخ يتجاوز حدود الجدول")
            return False

        changes: list[tuple[int, str, object]] = []
        parser = self._parser or _default_parser
        for row_offset, values in enumerate(matrix):
            row_index = start_row + row_offset
            staged_row = dict(self._rows[row_index])
            for col_offset, text in enumerate(values):
                col_index = start_col + col_offset
                col = self._columns[col_index]
                if not col.editable:
                    continue
                try:
                    value = self._parse_pasted_value(col, text, row_index, parser, staged_row)
                except Exception as exc:
                    self._report_edit_error(f"قيمة غير صالحة في الصف {row_index + 1}، عمود «{col.label}»: {exc}")
                    return False
                staged_row[col.key] = value
                if self._rows[row_index].get(col.key) != value:
                    changes.append((row_index, col.key, value))
        if not changes:
            return True
        return self._apply_user_changes(changes, record_history=True)

    def _parse_pasted_value(self, col: GridColumn, text: str, row_index: int, parser, row_values: dict) -> object:
        if col.kind == "picker":
            clean = text.strip()
            if not clean:
                return None
            options = self._picker_options(col, row_values) if self._picker_options else []
            exact = []
            for value, label in options:
                if (clean == str(label) or clean == str(value)) and not any(value == match for match in exact):
                    exact.append(value)
            if not exact and " · " not in clean:
                for value, label in options:
                    if clean == str(label).split(" · ", 1)[0] and not any(value == match for match in exact):
                        exact.append(value)
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                raise ValueError("الاسم متكرر؛ انسخ القيمة المميزة من القائمة")
            raise ValueError("لا تطابق مادة أو وحدة متاحة")
        return self._parse_value(col, text, parser)

    @staticmethod
    def _parse_value(col: GridColumn, text: str, parser) -> object:
        value = parser(col, text)
        if col.kind in ("number", "money"):
            try:
                finite = math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("أدخل رقمًا صالحًا") from exc
            if not finite:
                raise ValueError("أدخل رقمًا محدودًا")
        return value

    def _report_edit_error(self, message: str) -> None:
        if self._on_edit_error is not None:
            self._on_edit_error(message)
            return
        if self._page is not None:
            try:
                self._page.open(ft.SnackBar(content=ft.Text(message)))
                return
            except Exception:
                pass
        logging.getLogger(__name__).warning("%s", message)

    def _row_snapshots(self, row_indices: set[int]) -> dict[int, dict]:
        return {index: dict(self._rows[index]) for index in row_indices if 0 <= index < len(self._rows)}

    def _apply_user_changes(self, changes: list[tuple[int, str, object]], *, record_history: bool) -> bool:
        snapshots = self._row_snapshots({row for row, _, _ in changes})
        try:
            for row_index, key, value in changes:
                row = self._rows[row_index]
                old = row.get(key)
                if old == value:
                    continue
                row[key] = value
                if self._on_cell_commit is not None:
                    self._on_cell_commit(row_index, key, value)
        except Exception as exc:
            self._restore_rows(snapshots)
            self._report_edit_error(f"تعذر تطبيق التعديل: {exc}")
            return False

        if record_history:
            recorded: list[tuple[int, str, object, object]] = []
            for row_index, before in snapshots.items():
                after = self._rows[row_index]
                keys = list(before)
                keys.extend(key for key in after if key not in before)
                recorded.extend(
                    (row_index, key, before.get(key), after.get(key))
                    for key in keys
                    if before.get(key) != after.get(key)
                )
            self._record_history(recorded)
        self._render_body()
        self._safe_update(self._body)
        return True

    def _restore_rows(self, snapshots: dict[int, dict]) -> None:
        changed: list[tuple[int, str, object]] = []
        for row_index, before in snapshots.items():
            current = self._rows[row_index]
            keys = set(current) | set(before)
            changed.extend((row_index, key, before.get(key)) for key in keys if current.get(key) != before.get(key))
            self._rows[row_index] = dict(before)
        if self._on_cell_commit is not None:
            for row_index, key, value in reversed(changed):
                try:
                    self._on_cell_commit(row_index, key, value)
                except Exception:
                    logging.getLogger(__name__).exception("Failed to persist grid rollback for %s", key)
        self._render_body()
        self._safe_update(self._body)

    def _record_history(self, changes: list[tuple[int, str, object, object]]) -> None:
        if not changes:
            return
        self._history = self._history[: self._history_cursor]
        self._history.append(changes)
        if len(self._history) > _HISTORY_LIMIT:
            self._history.pop(0)
        self._history_cursor = len(self._history)

    def _replay_history(self, *, redo: bool) -> bool:
        if redo:
            if self._history_cursor >= len(self._history):
                return False
            entry = self._history[self._history_cursor]
            changes = [(row, key, new if redo else old) for row, key, old, new in entry]
        else:
            if self._history_cursor <= 0:
                return False
            entry = self._history[self._history_cursor - 1]
            changes = [(row, key, old) for row, key, old, _new in entry]
        if not self._apply_user_changes(changes, record_history=False):
            return False
        self._history_cursor += 1 if redo else -1
        return True

    def _display(self, col: GridColumn, value, row: dict) -> ft.Control:
        if self._formatter is not None:
            out = self._formatter(col, value, row)
            if isinstance(out, ft.Control):
                return out
            if isinstance(out, tuple) and len(out) == 3:
                text, fg, bg = out
                return status_pill(str(text), fg, bg, size=10)
            if out is None:
                return self._text(col, _default_text(col, value))
            return self._text(col, str(out))
        return self._text(col, _default_text(col, value))

    def _text(self, col: GridColumn, value) -> ft.Text:
        """Cell text; money/number cells get a medium weight so a column of
        figures scans as a tidy block instead of a wall of thin digits."""
        weight = ft.FontWeight.W_600 if col.kind in ("money", "number") else None
        return ft.Text(str(value), size=12.5, color=Colors.TEXT_PRIMARY, weight=weight)

    def _make_cell(self, r: int, c: int, col: GridColumn, row: dict) -> ft.Container:
        box = ft.Container(
            self._display(col, row.get(col.key), row),
            height=self._row_height,
            alignment=self._col_align(col),
            padding=ft.padding.symmetric(horizontal=12),
            border_radius=Radius.SM,
            on_click=lambda _, rr=r, cc=c: self._on_cell_click(rr, cc),
            **self._cell_kwargs(col),
        )
        self._style_cell_selection(r, c, box)
        self._cell_boxes[(r, c)] = box
        return box

    def _check_cell(self, r: int) -> ft.Control:
        button = ft.IconButton(
            icon=ft.Icons.CHECK_BOX if r in self._checked else ft.Icons.CHECK_BOX_OUTLINE_BLANK,
            icon_size=18,
            icon_color=Colors.PRIMARY if r in self._checked else Colors.TEXT_FAINT,
            tooltip="تحديد الصف",
            on_click=lambda _, rr=r: self._toggle_check(rr),
        )
        self._check_buttons.append(button)
        return ft.Container(button, width=40, height=self._row_height, alignment=ft.alignment.center)

    def _menu_items(self, row: dict) -> list[ft.Control]:
        items: list[ft.Control] = []
        if self._on_row_menu is None:
            return items
        for entry in self._on_row_menu(row):
            if entry is None:
                # This Flet version has no PopupMenuDivider; separators are
                # simply skipped.
                continue
            label, icon, callback = entry[0], entry[1], entry[2]
            items.append(
                ft.PopupMenuItem(
                    text=label,
                    icon=icon,
                    on_click=lambda _, cb=callback: cb(),
                )
            )
        return items

    def _make_row(self, r: int, row: dict) -> ft.Container:
        cells: list[ft.Control] = []
        if self._on_row_expand is not None:
            expanded = r in self._expanded
            cells.append(
                ft.Container(
                    ft.IconButton(
                        icon=ft.Icons.KEYBOARD_ARROW_UP_ROUNDED if expanded else ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED,
                        icon_size=16,
                        icon_color=Colors.TEXT_FAINT,
                        tooltip="تفاصيل سريعة",
                        on_click=lambda _, rr=r: self._toggle_expand(rr),
                    ),
                    width=32,
                    height=self._row_height,
                    alignment=ft.alignment.center,
                )
            )
        if self._selectable:
            cells.append(self._check_cell(r))
        cells.extend(self._make_cell(r, c, col, row) for c, col in enumerate(self._columns))
        if self._on_row_activate is not None:
            cells.append(
                ft.Container(
                    ft.IconButton(
                        icon=self._activate_icon,
                        icon_size=16,
                        icon_color=Colors.TEXT_FAINT,
                        tooltip=self._activate_tooltip,
                        on_click=lambda _, rr=r: self.activate_row(rr),
                    ),
                    width=48,
                    height=self._row_height,
                    alignment=ft.alignment.center,
                )
            )
        if self._on_row_menu is not None:
            cells.append(
                ft.Container(
                    ft.PopupMenuButton(
                        icon=ft.Icons.MORE_VERT,
                        icon_size=16,
                        icon_color=Colors.TEXT_FAINT,
                        tooltip="إجراءات",
                        items=self._menu_items(row),
                    ),
                    width=40,
                    height=self._row_height,
                    alignment=ft.alignment.center,
                )
            )
        box = ft.Container(
            ft.Row(cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            bgcolor=self._row_bg_for(r),
            on_hover=lambda e, rr=r: self._on_row_hover(e, rr),
        )
        self._row_boxes.append(box)
        return box

    def _row_bg_for(self, r: int) -> str:
        """Zebra stripe: a faint tint on alternate rows aids horizontal
        tracking across a wide table without the heavier card borders.

        A view-supplied ``row_bg`` hook (e.g. overdue invoices, out-of-stock
        items) wins over the stripe, and an explicit check-mark selection
        wins over both.
        """
        if r in self._checked:
            return Colors.PRIMARY_BG
        if self._row_bg is not None and 0 <= r < len(self._rows):
            try:
                tint = self._row_bg(self._rows[r])
            except Exception:
                tint = None
            if tint:
                return tint
        if self._striped and r % 2 == 1:
            return Colors.BACKGROUND_ALT
        return Colors.WHITE

    def _toggle_expand(self, r: int) -> None:
        if r in self._expanded:
            self._expanded.discard(r)
        else:
            self._expanded.add(r)
        self._render_body()
        self._safe_update(self._body)

    def _group_banner(self, label: str) -> ft.Control:
        return ft.Container(
            ft.Text(label, size=11, weight=ft.FontWeight.W_700, color=Colors.TEXT_SECONDARY),
            height=30,
            padding=ft.padding.symmetric(horizontal=14),
            alignment=ft.alignment.center_right,
            bgcolor=Colors.BACKGROUND,
            border=ft.border.only(top=ft.BorderSide(1, Colors.BORDER), bottom=ft.BorderSide(1, Colors.BORDER)),
        )

    def _build_footer(self) -> ft.Control | None:
        if not self._footer_values:
            return None
        cells: list[ft.Control] = []
        if self._on_row_expand is not None:
            cells.append(ft.Container(width=32, height=self._header_height))
        if self._selectable:
            cells.append(ft.Container(width=40, height=self._header_height))
        for col in self._columns:
            text = str(self._footer_values.get(col.key, "") or "")
            cells.append(
                ft.Container(
                    ft.Text(text, size=12, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                    height=self._header_height,
                    alignment=self._col_align(col),
                    padding=ft.padding.symmetric(horizontal=12),
                    **self._cell_kwargs(col),
                )
            )
        trail = self._trailing_width()
        if trail:
            cells.append(ft.Container(width=trail, height=self._header_height))
        return ft.Container(
            ft.Row(cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=Colors.BACKGROUND_ALT,
            border=ft.border.only(top=ft.BorderSide(1, Colors.BORDER_STRONG)),
        )

    def _refresh_footer(self) -> None:
        footer = self._build_footer()
        self._footer_slot.content = footer
        self._footer_slot.visible = footer is not None
        self._safe_update(self._footer_slot)

    def _render_body(self) -> None:
        self._cell_boxes.clear()
        self._row_boxes.clear()
        self._check_buttons.clear()
        self._edit_field = None
        # Clamp the cursor to the new row count *before* building cells so the
        # active-column highlight lands on a real cell.
        self.nav.set_rows(len(self._rows))
        controls: list[ft.Control] = []
        if self._top_row is not None:
            controls.append(self._top_row)
        if not self._rows:
            controls.append(
                ft.Container(
                    ft.Column(
                        [
                            ft.Container(
                                ft.Icon(ft.Icons.TABLE_ROWS_OUTLINED, size=28, color=Colors.PRIMARY),
                                width=62,
                                height=62,
                                bgcolor=Colors.PRIMARY_BG,
                                border_radius=31,
                                alignment=ft.alignment.center,
                            ),
                            ft.Text(self._empty_text, size=13, color=Colors.TEXT_SECONDARY),
                        ],
                        spacing=12,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.padding.symmetric(vertical=52, horizontal=20),
                    alignment=ft.alignment.center,
                )
            )
        else:
            last_group: str | None = None
            for r, row in enumerate(self._rows):
                if self._group_by is not None:
                    try:
                        group = self._group_by(row)
                    except Exception:
                        group = None
                    if group and group != last_group:
                        controls.append(self._group_banner(str(group)))
                        last_group = str(group)
                controls.append(self._make_row(r, row))
                if r in self._expanded and self._on_row_expand is not None:
                    try:
                        panel = self._on_row_expand(row)
                    except Exception:
                        panel = None
                    if panel is not None:
                        controls.append(
                            ft.Container(
                                panel,
                                padding=ft.padding.only(left=56, right=16, top=8, bottom=10),
                                bgcolor=Colors.BACKGROUND,
                                border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
                            )
                        )
        self._body.controls = controls
        self._painted_selection = self._selection_cells() if self._cell_boxes else set()
        self._prev_cell = self.nav.cell if self._cell_boxes else None
        self._sync_select_all()

    # ---------------------------------------------------------------- reload
    def row_at(self, index: int) -> dict | None:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def set_rows(self, rows: list[dict]) -> None:
        # Views call this on every refresh (data unchanged on most of them,
        # e.g. after set_hidden_keys/set_group_by bookkeeping). Rebuilding
        # the body for identical data is the dominant cost of a refresh
        # (~33ms per 60 rows, measured), so bail out early -- and keep the
        # user's expansion/selection state to boot.
        incoming = [dict(r) for r in rows]
        if incoming == self._rows:
            return
        self._rows = incoming
        self._history.clear()
        self._history_cursor = 0
        self._range_anchor = None
        self._painted_selection.clear()
        # A new result set (search/filter/sort) invalidates old row indices.
        self._checked.clear()
        self._expanded.clear()
        self._render_body()
        self._safe_update(self._body)
        self._emit_selection()

    def set_row_height(self, height: int) -> None:
        """Switch density ("comfortable" 48px vs "compact" 36px)."""
        self._row_height = max(28, int(height))
        self._render_body()
        self._safe_update(self._body)

    @property
    def rows(self) -> list[dict]:
        """Snapshot of the current data (copies, so callers can't mutate it)."""
        return [dict(r) for r in self._rows]

    def set_footer(self, values: dict[str, str] | None) -> None:
        """Show a bold summary row (e.g. column totals) under the data.

        ``values`` maps a column key to the text shown in that column's
        footer cell. Pass ``None`` to remove the footer.
        """
        self._footer_values = dict(values) if values else None
        self._refresh_footer()

    def add_row(self, row: dict | None = None) -> int:
        """Append a (blank) row, move the cursor to its first editable cell
        and arm the grid. Returns the new row's index.

        Used by the bulk-entry sheets: the user keeps appending rows and
        saves the whole batch at once, so a blank row must be ready for
        typing without a click.
        """
        blank = {col.key: "" for col in self._columns}
        if row:
            # Only keep keys that map to real columns, so a stray field can
            # never leak into the row that gets saved.
            blank.update({k: v for k, v in row.items() if k in blank})
        self._rows.append(blank)
        index = len(self._rows) - 1
        # Resize the model *before* selecting, otherwise select() clamps the
        # cursor against the old (one-shorter) row count.
        self.nav.set_rows(len(self._rows))
        editable = self.nav.editable_cols
        self.nav.select(index, editable[0] if editable else 0)
        self._armed = True
        self._range_anchor = None
        self._prev_cell = None
        self._render_body()
        self._safe_update(self._body)
        return index

    def remove_row(self, index: int) -> None:
        """Drop a row (e.g. a blank entry the user no longer wants)."""
        if not (0 <= index < len(self._rows)):
            return
        del self._rows[index]
        self._history.clear()
        self._history_cursor = 0
        self._range_anchor = None
        self._painted_selection.clear()
        self._checked = {i if i < index else i - 1 for i in self._checked if i != index}
        self.nav.editing = False
        self._render_body()
        self._safe_update(self._body)
        self._emit_selection()

    def arm(self) -> None:
        """Enable keyboard navigation without requiring an initial click."""
        self._armed = True
        self._shortcuts_enabled = True

    def set_cell(self, index: int, key: str, value, *, notify: bool = True) -> None:
        """Write ``value`` into one cell and repaint its row.

        Used by picker cells and by view callbacks that update dependent cells.
        """
        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        old = row.get(key)
        if value == old:
            return
        row[key] = value
        if notify and self._on_cell_commit is not None:
            try:
                self._on_cell_commit(index, key, value)
            except Exception:
                row[key] = old
        self._render_row(index)
        self._safe_update(self._body)

    # ---------------------------------------------------------- multi-select
    def _toggle_check(self, r: int) -> None:
        if r in self._checked:
            self._checked.discard(r)
        else:
            self._checked.add(r)
        self._paint_check(r)
        self._sync_select_all()
        self._emit_selection()

    def _toggle_all(self) -> None:
        if self._rows and len(self._checked) == len(self._rows):
            self._checked.clear()
        else:
            self._checked = set(range(len(self._rows)))
        for r in range(len(self._rows)):
            self._paint_check(r)
        self._sync_select_all()
        self._emit_selection()

    def _paint_check(self, r: int) -> None:
        checked = r in self._checked
        if 0 <= r < len(self._check_buttons):
            button = self._check_buttons[r]
            button.icon = ft.Icons.CHECK_BOX if checked else ft.Icons.CHECK_BOX_OUTLINE_BLANK
            button.icon_color = Colors.PRIMARY if checked else Colors.TEXT_FAINT
            self._safe_update(button)
        if 0 <= r < len(self._row_boxes):
            box = self._row_boxes[r]
            box.bgcolor = self._row_bg_for(r)
            self._safe_update(box)

    def _sync_select_all(self) -> None:
        if self._select_all_button is None:
            return
        self._select_all_button.icon = (
            ft.Icons.CHECK_BOX
            if self._rows and len(self._checked) == len(self._rows)
            else ft.Icons.CHECK_BOX_OUTLINE_BLANK
        )
        self._safe_update(self._select_all_button)

    def selected_rows(self) -> list[dict]:
        return [dict(self._rows[i]) for i in sorted(self._checked) if 0 <= i < len(self._rows)]

    def clear_selection(self) -> None:
        self._checked.clear()
        for r in range(len(self._rows)):
            self._paint_check(r)
        self._sync_select_all()
        self._emit_selection()

    def _emit_selection(self) -> None:
        if self._on_selection_change is None:
            return
        try:
            self._on_selection_change(self.selected_rows())
        except Exception:
            pass

    # ------------------------------------------------------------- selection
    def _on_cell_click(self, r: int, c: int) -> None:
        if self._picker_control is not None and self.nav.cell != (r, c):
            self._picker_close()
        had_range = self._range_anchor is not None
        self._range_anchor = None
        if had_range and self.nav.cell == (r, c):
            self._paint_selection()
        was_armed = self._armed
        self._armed = True
        self._shortcuts_enabled = True
        if self.nav.cell == (r, c):
            # First click on an already-active cell only arms the grid;
            # a second click opens the editor (click-to-select, click-again
            # -to-edit -- so the default-highlighted first cell doesn't pop
            # an editor on the very first click).
            if was_armed and self._edit_field is None and self._picker_control is None and self.nav.is_editable:
                self._begin_edit()
            return
        self.nav.select(r, c)
        self._paint_selection()

    def disarm(self) -> None:
        """Stop handling navigation keys (e.g. while a view's search box has
        focus) until the user clicks a cell again."""
        self._armed = False
        self._shortcuts_enabled = False

    def _on_row_hover(self, e: ft.ControlEvent, r: int) -> None:
        # Do not fight the active row's own highlight.
        if self.nav.row == r:
            return
        box = self._row_boxes[r]
        box.bgcolor = Colors.PRIMARY_BG if e.data == "true" else self._row_bg_for(r)
        self._safe_update(box)

    def _paint_selection(self) -> None:
        selected = self._selection_cells()
        targets = self._painted_selection | selected
        if self._prev_cell is not None:
            targets.add(self._prev_cell)
        for (r, c) in targets:
            box = self._cell_boxes.get((r, c))
            if box is None:
                continue
            self._style_cell_selection(r, c, box)
            self._safe_update(box)
        self._painted_selection = selected
        self._prev_cell = self.nav.cell

    def _selection_cells(self) -> set[tuple[int, int]]:
        if self._range_anchor is None:
            return {self.nav.cell}
        anchor_row, anchor_col = self._range_anchor
        row_start, row_end = sorted((anchor_row, self.nav.row))
        col_start, col_end = sorted((anchor_col, self.nav.col))
        return {(row, col) for row in range(row_start, row_end + 1) for col in range(col_start, col_end + 1)}

    def _style_cell_selection(self, row: int, col: int, box: ft.Container) -> None:
        active = (row, col) == self.nav.cell
        selected = (row, col) in self._selection_cells()
        box.bgcolor = Colors.PRIMARY_BG if selected else None
        box.border = ft.border.all(2, Colors.PRIMARY) if active else ft.border.all(1, Colors.PRIMARY_BORDER) if selected else None

    # ------------------------------------------------------------------ edit
    def _begin_edit(self, prefill: str | None = None) -> None:
        r, c = self.nav.cell
        col = self._columns[c] if 0 <= c < len(self._columns) else None
        box = self._cell_boxes.get((r, c))
        if col is None or box is None or not col.editable:
            return
        if col.kind == "picker":
            if self._picker_options is not None:
                self._begin_picker(r, col, box)
                return
            # Legacy picker cells may still delegate to a view-level selector.
            if self._on_pick is not None:
                self._on_pick(r, col)
            return
        self.nav.begin_edit()
        if prefill is not None:
            edit_text = prefill
        else:
            current = self._rows[r].get(col.key)
            edit_text = None
            if self._edit_formatter is not None:
                out = self._edit_formatter(col, current, self._rows[r])
                if out is not None:
                    edit_text = str(out)
            if edit_text is None:
                edit_text = _default_text(col, current)
        field = ft.TextField(
            value=edit_text,
            dense=True,
            text_size=12,
            autofocus=True,
            border_radius=Radius.SM,
            border=ft.border.all(2, Colors.PRIMARY),
            bgcolor=Colors.WHITE,
            content_padding=ft.padding.symmetric(horizontal=8, vertical=6),
            text_align=self._edit_align(col),
            keyboard_type=ft.KeyboardType.NUMBER if col.kind in ("number", "money") else ft.KeyboardType.TEXT,
            on_submit=lambda _: self._commit_edit(move="next"),
            on_blur=lambda _: self._commit_edit(),
        )
        box.bgcolor = Colors.WHITE
        box.border = ft.border.all(2, Colors.PRIMARY)
        box.content = field
        self._edit_field = field
        self._safe_update(box)
        try:
            v = field.value or ""
            try:
                if prefill is not None:
                    field.selection = ft.TextSelection(base_offset=len(v), extent_offset=len(v))
                else:
                    field.selection = ft.TextSelection(base_offset=0, extent_offset=len(v))
            except Exception:
                pass
            field.focus()
        except Exception:
            pass

    def _begin_picker(self, row_index: int, col: GridColumn, box: ft.Container) -> None:
        options = self._picker_options(col, self._rows[row_index]) if self._picker_options else []
        if not options:
            if self._on_pick is not None:
                self._on_pick(row_index, col)
            return
        if self._page is None:
            # No page to host a dialog: fall back to the legacy view selector.
            if self._on_pick is not None:
                self._on_pick(row_index, col)
            return
        self._picker_row = row_index
        self._picker_key = col.key
        self._picker_items = [(value, str(label)) for value, label in options]
        self._picker_highlight = 0
        self.nav.begin_edit()

        search = ft.TextField(
            hint_text="اكتب للبحث…",
            autofocus=True,
            dense=True,
            border_radius=Radius.SM,
            prefix_icon=ft.Icons.SEARCH,
            on_change=lambda e: self._picker_filter(e.control.value),
            on_submit=lambda _: self._picker_commit(),
        )
        self._picker_search = search
        self._picker_list = ft.Column([], spacing=2, scroll=ft.ScrollMode.AUTO, height=320)
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(col.label or "اختر"),
            content=ft.Column([search, self._picker_list], spacing=10, tight=True, width=440),
            actions=[
                ft.TextButton("إلغاء", on_click=lambda _: self._picker_close()),
                ft.FilledButton("اختيار", on_click=lambda _: self._picker_commit()),
            ],
            on_dismiss=lambda _: self._picker_close(),
        )
        self._picker_dialog = dialog
        self._picker_control = dialog  # truthy flag while open
        self._picker_filter("")
        try:
            self._page.open(dialog)
        except Exception:
            pass

    def _picker_filter(self, query: str) -> None:
        q = (query or "").strip().casefold()
        self._picker_visible = [
            (value, label) for (value, label) in self._picker_items if not q or q in label.casefold()
        ]
        self._picker_highlight = 0
        self._picker_render()

    def _picker_render(self) -> None:
        controls: list[ft.Control] = []
        for i, (_value, label) in enumerate(self._picker_visible):
            selected = i == self._picker_highlight
            controls.append(
                ft.Container(
                    ft.Text(label, size=13, color=Colors.PRIMARY if selected else Colors.TEXT_PRIMARY),
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    bgcolor=Colors.PRIMARY_BG if selected else None,
                    border_radius=Radius.SM,
                    ink=True,
                    on_click=lambda _, k=i: self._picker_choose(k),
                )
            )
        if not controls:
            controls.append(ft.Text("لا نتائج مطابقة", size=12, color=Colors.TEXT_FAINT))
        if self._picker_list is not None:
            self._picker_list.controls = controls
            try:
                self._picker_list.update()
            except Exception:
                pass

    def _picker_move(self, delta: int) -> None:
        total = len(self._picker_visible)
        if total:
            self._picker_highlight = (self._picker_highlight + delta) % total
            self._picker_render()

    def _picker_choose(self, index: int) -> None:
        if 0 <= index < len(self._picker_visible):
            self._picker_highlight = index
            self._picker_commit()

    def _picker_commit(self) -> None:
        if not self._picker_visible:
            self._picker_close()
            return
        value = self._picker_visible[self._picker_highlight][0]
        row, key = self._picker_row, self._picker_key
        dialog = self._picker_dialog
        self._picker_dialog = None
        self._picker_control = None
        self._picker_items = []
        self._picker_visible = []
        self._picker_search = None
        self._picker_list = None
        self._picker_row = None
        self._picker_key = None
        self.nav.editing = False
        if dialog is not None:
            try:
                self._page.close(dialog)
            except Exception:
                pass
        if row is None or key is None:
            return
        if self._rows[row].get(key) == value:
            self._render_row(row)
            self._safe_update(self._body)
        else:
            self._apply_user_changes([(row, key, value)], record_history=True)

    def _picker_close(self) -> None:
        dialog = self._picker_dialog
        self._picker_dialog = None
        self._picker_control = None
        self._picker_items = []
        self._picker_visible = []
        self._picker_search = None
        self._picker_list = None
        self.nav.editing = False
        if dialog is not None:
            try:
                self._page.close(dialog)
            except Exception:
                pass
        self._render_row(self.nav.row)
        self._safe_update(self._body)

    def _commit_edit(self, *, move: str | None = None) -> None:
        field = self._edit_field
        if field is None:
            return
        r, c = self.nav.cell
        col = self._columns[c]
        try:
            value = self._parse_value(col, field.value or "", self._parser or _default_parser)
        except ValueError:
            # Keep the editor open and flag the cell instead of silently
            # throwing the typed value away -- the user can fix it in place
            # or press Escape to cancel.
            field.border = ft.border.all(2, Colors.DANGER)
            field.error_text = "قيمة غير صحيحة"
            self._safe_update(field)
            return
        self._edit_field = None
        self.nav.editing = False
        old = self._rows[r].get(col.key)
        if value is not None and value != old:
            if not self._apply_user_changes([(r, col.key, value)], record_history=True):
                return
        self._render_row(r)
        self._safe_update(self._body)
        # Enter commits and advances to the next editable cell (wrapping to
        # the next row, appending a row at the end).
        if move == "next":
            self._advance_forward()
        elif move == "down" and self.nav.move_down():
            self._paint_selection()

    def _advance_forward(self) -> None:
        """Move to the next editable cell; at the end of a row wrap to the
        first editable cell of the next row, and at the very end of the grid
        append a fresh row (editable grids only)."""
        if self.nav.tab(False):
            self._paint_selection()
            return
        if self.nav.editable_cols:
            self.add_row()

    def _cancel_edit(self) -> None:
        if self._edit_field is None:
            return
        self._edit_field = None
        self.nav.editing = False
        self._render_row(self.nav.row)
        self._safe_update(self._body)

    def _clear_active(self) -> None:
        cells = self._selection_cells()
        changes = []
        for r, c in sorted(cells):
            if not (0 <= c < len(self._columns)):
                continue
            col = self._columns[c]
            if not col.editable:
                continue
            blank = None if col.kind == "picker" else "" if col.kind == "text" else 0.0
            if self._rows[r].get(col.key) != blank:
                changes.append((r, col.key, blank))
        if changes:
            self._apply_user_changes(changes, record_history=True)

    def _render_row(self, r: int) -> None:
        if not (0 <= r < len(self._rows)):
            return
        row_box = self._row_boxes[r]
        row = self._rows[r]
        for c, col in enumerate(self._columns):
            box = self._cell_boxes.get((r, c))
            if box is None:
                continue
            box.content = self._display(col, row.get(col.key), row)
            self._style_cell_selection(r, c, box)
            box.padding = ft.padding.symmetric(horizontal=12)
        row_box.bgcolor = self._row_bg_for(r)
        self._safe_update(row_box)

    # ----------------------------------------------------------- row action
    def activate_row(self, index: int) -> None:
        row = self.row_at(index)
        if row is not None and self._on_row_activate is not None:
            self._on_row_activate(row)

    @staticmethod
    def _safe_update(control: ft.Control) -> None:
        try:
            control.update()
        except Exception:
            pass


__all__ = ["DataGrid"]
