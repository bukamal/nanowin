"""Pure-Python navigation model for the editable :class:`DataGrid`.

Deliberately free of any ``flet`` import: this module only decides *which
cell is active* and *where the cursor goes next* for a key press. Keeping
the keyboard contract framework-free means it can be unit-tested without a
running Flet app -- the same reasoning behind ``core.router``.

The interaction model is the spreadsheet one users already know from Excel:

* **Arrows** move the selection over every cell (read-only ones included).
* **Tab / Shift+Tab** hop over *editable* cells only, wrapping into the
  next/previous row, so data entry never lands on a computed column.
* **Enter** starts editing the active cell (if editable); a second Enter
  commits. **Escape** cancels without writing.
* **Home / End** jump to the first/last column, **Ctrl+Home/End** were
  intentionally left out -- the model stays small and predictable.
* **PageUp / PageDown** move by a caller-supplied page height.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GridColumn:
    """Static description of one grid column.

    ``width=None`` means "flex to fill the leftover space" (the renderer
    expands exactly one such column); an int pins a fixed pixel width.
    ``editable`` marks the cells Tab stops on and Enter may open for input.
    """

    key: str
    label: str
    width: int | None = None
    align: str = "start"  # start | center | end
    editable: bool = False
    kind: str = "text"  # text | number | money | badge
    sortable: bool = True


class GridNavigator:
    """Tracks the active cell and editing state of a grid.

    The model never stores cell *values* -- the view owns those. It only
    answers "where am I" and "where does this key take me", which keeps it
    trivial to reason about and to test.
    """

    def __init__(
        self,
        columns: list[GridColumn] | tuple[GridColumn, ...],
        row_count: int = 0,
    ):
        self.columns: list[GridColumn] = list(columns)
        self.row_count = max(0, int(row_count))
        self.row = 0
        self.col = 0
        self.editing = False

    # ------------------------------------------------------------------ state
    @property
    def editable_cols(self) -> list[int]:
        return [i for i, c in enumerate(self.columns) if c.editable]

    @property
    def has_rows(self) -> bool:
        return self.row_count > 0 and bool(self.columns)

    @property
    def column(self) -> GridColumn | None:
        if not self.columns:
            return None
        return self.columns[self.col]

    @property
    def key(self) -> str | None:
        col = self.column
        return col.key if col is not None else None

    @property
    def cell(self) -> tuple[int, int]:
        return (self.row, self.col)

    @property
    def is_editable(self) -> bool:
        col = self.column
        return bool(col is not None and col.editable)

    def set_rows(self, row_count: int) -> None:
        """Resize the grid (after a reload) and re-clamp the cursor."""
        self.row_count = max(0, int(row_count))
        self.editing = False
        self.clamp()

    def clamp(self) -> None:
        if self.columns:
            self.col = min(max(0, self.col), len(self.columns) - 1)
        else:
            self.col = 0
        if self.row_count:
            self.row = min(max(0, self.row), self.row_count - 1)
        else:
            self.row = 0

    def select(self, row: int, col: int) -> None:
        self.row = int(row)
        self.col = int(col)
        self.editing = False
        self.clamp()

    # ------------------------------------------------------------------ moves
    def move(self, dr: int = 0, dc: int = 0) -> bool:
        """Move the selection; returns True when the active cell changed."""
        before = (self.row, self.col)
        self.editing = False
        self.row += int(dr)
        self.col += int(dc)
        self.clamp()
        return (self.row, self.col) != before

    def move_up(self) -> bool:
        return self.move(dr=-1)

    def move_down(self) -> bool:
        return self.move(dr=1)

    def move_left(self) -> bool:
        return self.move(dc=-1)

    def move_right(self) -> bool:
        return self.move(dc=1)

    def home(self) -> bool:
        return self.move(dc=-len(self.columns))

    def end(self) -> bool:
        return self.move(dc=len(self.columns))

    def first(self) -> bool:
        return self.move(dr=-self.row_count)

    def last(self) -> bool:
        return self.move(dr=self.row_count)

    def page(self, direction: int, page_size: int) -> bool:
        return self.move(dr=int(direction) * max(1, int(page_size)))

    def tab(self, shift: bool = False) -> bool:
        """Advance to the next/previous *editable* cell across the grid.

        Scanning is flat (row-major) and stops at the grid edges rather than
        wrapping, so Tab at the last field simply stays put -- no surprising
        jump back to the top-left mid-entry.
        """
        editable = self.editable_cols
        if not editable or not self.has_rows:
            return False
        ncols = len(self.columns)
        total = self.row_count * ncols
        step = -1 if shift else 1
        pos = self.row * ncols + self.col
        for _ in range(total):
            pos += step
            if pos < 0 or pos >= total:
                return False
            r, c = divmod(pos, ncols)
            if c in editable:
                self.row, self.col, self.editing = r, c, False
                return True
        return False

    # ------------------------------------------------------------------ edit
    def begin_edit(self) -> bool:
        """Enter edit mode for the active cell; False if it is read-only."""
        if not self.is_editable:
            return False
        self.editing = True
        return True

    def commit(self) -> bool:
        was_editing = self.editing
        self.editing = False
        return was_editing

    def cancel(self) -> bool:
        was_editing = self.editing
        self.editing = False
        return was_editing

    def enter(self) -> str:
        """Resolve an Enter press into ``"edit"``, ``"commit"`` or ``"none"``."""
        if self.editing:
            self.commit()
            return "commit"
        if self.is_editable:
            self.begin_edit()
            return "edit"
        return "none"


__all__ = ["GridColumn", "GridNavigator"]
