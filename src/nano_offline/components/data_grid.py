"""Editable, keyboard-driven data grid for the desktop layout.

Renders like a spreadsheet: a sticky header row, zebra-striped body rows,
a highlighted active cell, and inline editing opened with Enter (or a
second click) on an editable column. Navigation semantics live in the
framework-free :mod:`nano_offline.components.grid_model`; this module is
only the Flet rendering + key wiring around it.

Why the "selection highlight" model instead of making every cell a
``TextField``: a grid of live text inputs needs focus juggling on every
arrow key and swallows keystrokes in ways that are hard to reason about.
Here only the *one* cell being edited owns a ``TextField`` -- arrows move
a highlight (no focus needed), and Enter materialises an input just for
that cell. That keeps the common case (scan/navigate/edit a price) robust.
"""

from __future__ import annotations

from typing import Callable

import flet as ft

from nano_offline.core.theme import Colors, Radius
from nano_offline.components.grid_model import GridColumn, GridNavigator
from nano_offline.components.status_pill import status_pill


def _norm_key(key: str | None) -> str:
    """Normalise a Flet key string so matching does not depend on whether
    the client reports ``"Arrow Up"`` or ``"ArrowUp"`` / ``"arrowup"``."""
    return (key or "").strip().lower().replace(" ", "").replace("_", "")


_UP, _DOWN, _LEFT, _RIGHT = "arrowup", "arrowdown", "arrowleft", "arrowright"
_PAGE_UP, _PAGE_DOWN = "pageup", "pagedown"


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
        return float(cleaned)  # ValueError propagates -> edit is cancelled
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
        formatter: Callable[[GridColumn, object, dict], object] | None = None,
        parser: Callable[[GridColumn, str], object] | None = None,
        activate_icon: str = ft.Icons.OPEN_IN_NEW_ROUNDED,
        activate_tooltip: str = "فتح",
        row_height: int = 46,
        header_height: int = 40,
        empty_text: str = "لا توجد بيانات",
    ):
        self._columns = list(columns)
        self._rows: list[dict] = [dict(r) for r in (rows or [])]
        self._page = page
        self._on_cell_commit = on_cell_commit
        self._on_row_activate = on_row_activate
        self._on_pick = on_pick
        self._formatter = formatter
        self._parser = parser
        self._activate_icon = activate_icon
        self._activate_tooltip = activate_tooltip
        self._row_height = row_height
        self._header_height = header_height
        self._empty_text = empty_text

        self.nav = GridNavigator(self._columns, len(self._rows))
        self._cell_boxes: dict[tuple[int, int], ft.Container] = {}
        self._row_boxes: list[ft.Container] = []
        self._edit_field: ft.TextField | None = None
        self._prev_cell: tuple[int, int] | None = None
        # Keyboard nav only reacts once the user has actually engaged the
        # grid (clicked a cell); otherwise a page-level handler would also
        # fire while the search box has focus (e.g. Enter there would open a
        # cell editor). The view calls disarm() when its own fields focus.
        self._armed = False
        self._body = ft.Column(spacing=0)

        super().__init__(
            controls=[
                ft.Container(
                    self._build_header(),
                    bgcolor=Colors.BACKGROUND_ALT,
                    border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER_STRONG)),
                ),
                ft.Column([self._body], scroll=ft.ScrollMode.AUTO, expand=True),
            ],
            spacing=0,
            expand=True,
        )
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
        if self._edit_field is not None:
            # While a cell editor is open, Enter/Escape resolve it; every
            # other key is the user typing, so leave it to the TextField.
            key = _norm_key(e.key)
            if key == "enter":
                self._commit_edit()
            elif key in ("escape", "esc"):
                self._cancel_edit()
            return

        key = _norm_key(e.key)
        if getattr(e, "ctrl", False) and key == "k":
            opener = getattr(self._page, "_nano_open_command_palette", None)
            if callable(opener):
                opener()
            return
        if not self._armed:
            return

        shift = bool(getattr(e, "shift", False))
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
            if self.nav.enter() == "edit":
                self._begin_edit()
            return
        elif key in ("delete", "backspace"):
            self._clear_active()
            return
        else:
            return
        if moved:
            self._paint_selection()

    def _page_rows(self) -> int:
        page = self._page
        height = getattr(page, "height", None) if page is not None else None
        if height:
            return max(1, int(height // self._row_height) - 4)
        return 12

    # ------------------------------------------------------------------ build
    def _col_align(self, col: GridColumn):
        return {
            "start": ft.alignment.center_right,  # leading edge in RTL
            "center": ft.alignment.center,
            "end": ft.alignment.center_left,
        }.get(col.align, ft.alignment.center_right)

    def _cell_kwargs(self, col: GridColumn) -> dict:
        if col.width is None:
            return {"expand": True}
        return {"width": col.width}

    def _build_header(self) -> ft.Row:
        cells: list[ft.Control] = []
        for col in self._columns:
            cells.append(
                ft.Container(
                    ft.Text(col.label, size=11, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                    height=self._header_height,
                    alignment=self._col_align(col),
                    padding=ft.padding.symmetric(horizontal=10),
                    **self._cell_kwargs(col),
                )
            )
        if self._on_row_activate is not None:
            cells.append(ft.Container(width=48, height=self._header_height))
        return ft.Row(cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _display(self, col: GridColumn, value, row: dict) -> ft.Control:
        if self._formatter is not None:
            out = self._formatter(col, value, row)
            if isinstance(out, ft.Control):
                return out
            if isinstance(out, tuple) and len(out) == 3:
                text, fg, bg = out
                return status_pill(str(text), fg, bg)
            if out is None:
                return ft.Text(_default_text(col, value), size=12, color=Colors.TEXT_PRIMARY)
            return ft.Text(str(out), size=12, color=Colors.TEXT_PRIMARY)
        return ft.Text(_default_text(col, value), size=12, color=Colors.TEXT_PRIMARY)

    def _make_cell(self, r: int, c: int, col: GridColumn, row: dict) -> ft.Container:
        active = (r, c) == self.nav.cell
        box = ft.Container(
            self._display(col, row.get(col.key), row),
            height=self._row_height,
            alignment=self._col_align(col),
            padding=ft.padding.symmetric(horizontal=10),
            bgcolor=Colors.PRIMARY_BG if active else None,
            border=ft.border.all(2, Colors.PRIMARY) if active else None,
            on_click=lambda _, rr=r, cc=c: self._on_cell_click(rr, cc),
            **self._cell_kwargs(col),
        )
        self._cell_boxes[(r, c)] = box
        return box

    def _make_row(self, r: int, row: dict) -> ft.Container:
        cells = [self._make_cell(r, c, col, row) for c, col in enumerate(self._columns)]
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
        box = ft.Container(
            ft.Row(cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            bgcolor=Colors.WHITE,
            on_hover=lambda e, rr=r: self._on_row_hover(e, rr),
        )
        self._row_boxes.append(box)
        return box

    def _render_body(self) -> None:
        self._cell_boxes.clear()
        self._row_boxes.clear()
        self._edit_field = None
        # Clamp the cursor to the new row count *before* building cells so the
        # active-column highlight lands on a real cell.
        self.nav.set_rows(len(self._rows))
        controls: list[ft.Control] = []
        if not self._rows:
            controls.append(
                ft.Container(
                    ft.Column(
                        [
                            ft.Icon(ft.Icons.TABLE_ROWS_OUTLINED, size=34, color=Colors.TEXT_FAINT),
                            ft.Text(self._empty_text, size=13, color=Colors.TEXT_SECONDARY),
                        ],
                        spacing=8,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.padding.symmetric(vertical=48),
                    alignment=ft.alignment.center,
                )
            )
        else:
            for r, row in enumerate(self._rows):
                controls.append(self._make_row(r, row))
        self._body.controls = controls

    # ---------------------------------------------------------------- reload
    def row_at(self, index: int) -> dict | None:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = [dict(r) for r in rows]
        self._render_body()
        self._safe_update(self._body)

    @property
    def rows(self) -> list[dict]:
        """Snapshot of the current data (copies, so callers can't mutate it)."""
        return [dict(r) for r in self._rows]

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
        self._prev_cell = None
        self._render_body()
        self._safe_update(self._body)
        return index

    def remove_row(self, index: int) -> None:
        """Drop a row (e.g. a blank entry the user no longer wants)."""
        if not (0 <= index < len(self._rows)):
            return
        del self._rows[index]
        self.nav.editing = False
        self._render_body()
        self._safe_update(self._body)

    def arm(self) -> None:
        """Enable keyboard navigation without requiring an initial click."""
        self._armed = True

    def set_cell(self, index: int, key: str, value, *, notify: bool = True) -> None:
        """Write ``value`` into one cell and repaint its row.

        Used by picker cells (see ``on_pick``), where the value comes from a
        dialog rather than a typed TextField.
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

    # ------------------------------------------------------------- selection
    def _on_cell_click(self, r: int, c: int) -> None:
        was_armed = self._armed
        self._armed = True
        if self.nav.cell == (r, c):
            # First click on an already-active cell only arms the grid;
            # a second click opens the editor (click-to-select, click-again
            # -to-edit -- so the default-highlighted first cell doesn't pop
            # an editor on the very first click).
            if was_armed and self._edit_field is None and self.nav.is_editable:
                self._begin_edit()
            return
        self.nav.select(r, c)
        self._paint_selection()

    def disarm(self) -> None:
        """Stop handling navigation keys (e.g. while a view's search box has
        focus) until the user clicks a cell again."""
        self._armed = False

    def _on_row_hover(self, e: ft.ControlEvent, r: int) -> None:
        # Do not fight the active row's own highlight.
        if self.nav.row == r:
            return
        box = self._row_boxes[r]
        box.bgcolor = Colors.BACKGROUND_ALT if e.data == "true" else Colors.WHITE
        self._safe_update(box)

    def _paint_selection(self) -> None:
        targets = {self.nav.cell}
        if self._prev_cell is not None:
            targets.add(self._prev_cell)
        for (r, c) in targets:
            box = self._cell_boxes.get((r, c))
            if box is None:
                continue
            active = (r, c) == self.nav.cell
            box.bgcolor = Colors.PRIMARY_BG if active else None
            box.border = ft.border.all(2, Colors.PRIMARY) if active else None
            self._safe_update(box)
        self._prev_cell = self.nav.cell

    # ------------------------------------------------------------------ edit
    def _begin_edit(self) -> None:
        r, c = self.nav.cell
        col = self._columns[c] if 0 <= c < len(self._columns) else None
        box = self._cell_boxes.get((r, c))
        if col is None or box is None or not col.editable:
            return
        if col.kind == "picker":
            # A picker cell has no free-text editor: hand control to the view,
            # which opens a chooser and writes back via set_cell().
            if self._on_pick is not None:
                self._on_pick(r, col)
            return
        self.nav.begin_edit()
        current = self._rows[r].get(col.key)
        field = ft.TextField(
            value=_default_text(col, current),
            dense=True,
            text_size=12,
            autofocus=True,
            border_radius=Radius.SM,
            border=ft.border.all(2, Colors.PRIMARY),
            bgcolor=Colors.WHITE,
            content_padding=ft.padding.symmetric(horizontal=8, vertical=6),
            text_align=ft.TextAlign.RIGHT if col.align != "end" else ft.TextAlign.LEFT,
            keyboard_type=ft.KeyboardType.NUMBER if col.kind in ("number", "money") else ft.KeyboardType.TEXT,
            on_submit=lambda _: self._commit_edit(),
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
                field.selection = ft.TextSelection(base_offset=0, extent_offset=len(v))
            except Exception:
                pass
            field.focus()
        except Exception:
            pass

    def _commit_edit(self) -> None:
        field = self._edit_field
        if field is None:
            return
        self._edit_field = None
        self.nav.editing = False
        r, c = self.nav.cell
        col = self._columns[c]
        try:
            value = (self._parser or _default_parser)(col, field.value or "")
        except ValueError:
            value = None  # unparseable -> keep the old value
        old = self._rows[r].get(col.key)
        if value is not None and value != old:
            self._rows[r][col.key] = value
            if self._on_cell_commit is not None:
                try:
                    self._on_cell_commit(r, col.key, value)
                except Exception:
                    # A rejected save must not silently keep the new value.
                    self._rows[r][col.key] = old
                    self._render_row(r)
                    self._safe_update(self._body)
                    return
        self._render_row(r)
        self._safe_update(self._body)

    def _cancel_edit(self) -> None:
        if self._edit_field is None:
            return
        self._edit_field = None
        self.nav.editing = False
        self._render_row(self.nav.row)
        self._safe_update(self._body)

    def _clear_active(self) -> None:
        r, c = self.nav.cell
        col = self._columns[c] if 0 <= c < len(self._columns) else None
        if col is None or not col.editable:
            return
        blank = "" if col.kind == "text" else 0.0
        old = self._rows[r].get(col.key)
        if old == blank:
            return
        self._rows[r][col.key] = blank
        if self._on_cell_commit is not None:
            try:
                self._on_cell_commit(r, col.key, blank)
            except Exception:
                self._rows[r][col.key] = old
                self._render_row(r)
                self._safe_update(self._body)
                return
        self._render_row(r)
        self._safe_update(self._body)

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
            active = (r, c) == self.nav.cell
            box.bgcolor = Colors.PRIMARY_BG if active else None
            box.border = ft.border.all(2, Colors.PRIMARY) if active else None
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
