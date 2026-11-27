"""Grid display preferences.

Currently one switch: **fit-to-width**. When on, an editable table distributes
its columns proportionally across the available width (using Flutter's flex)
instead of fixed pixel widths, so revealing many columns squeezes them to fit
rather than letting the right-hand columns overflow and get clipped. Turn it
off to restore the old fixed-width behaviour.

Read from ``DataGrid`` via the page's attached ``ctx`` (best-effort); defaults
to on so the fix applies everywhere without wiring a setting through every view.
"""

from __future__ import annotations

FIT_WIDTH_KEY = "ui.grid_fit_width"
DEFAULT_FIT_WIDTH = True

# Vertical column separators. Rows already carry a bottom border (the
# horizontal gridline); this adds a right border to every cell so wide tables
# read as a proper grid instead of a stack of rows.
VERTICAL_LINES_KEY = "ui.grid_vertical_lines"
DEFAULT_VERTICAL_LINES = True


def fit_width(settings) -> bool:
    try:
        raw = settings.get(FIT_WIDTH_KEY, "1" if DEFAULT_FIT_WIDTH else "0")
    except Exception:
        return DEFAULT_FIT_WIDTH
    return str(raw).strip() == "1"


def vertical_lines(settings) -> bool:
    try:
        raw = settings.get(VERTICAL_LINES_KEY, "1" if DEFAULT_VERTICAL_LINES else "0")
    except Exception:
        return DEFAULT_VERTICAL_LINES
    return str(raw).strip() == "1"


__all__ = [
    "FIT_WIDTH_KEY",
    "DEFAULT_FIT_WIDTH",
    "fit_width",
    "VERTICAL_LINES_KEY",
    "DEFAULT_VERTICAL_LINES",
    "vertical_lines",
]
