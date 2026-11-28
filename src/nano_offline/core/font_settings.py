"""App-wide font preferences (family + base size).

Kept free of any ``flet`` import -- it only reads two ``settings`` rows and
exposes the curated choices, so the shell (to build ``page.theme``) and the
document/print services (to build CSS) can share exactly one source of truth.

The app already bundles an optional Arabic typeface (IBM Plex Sans Arabic,
see ``assets/fonts/README.md``); ``"auto"`` keeps using it when present and
otherwise falls back to the platform default. Every other choice is a font
family that ships with Windows, so nothing here needs a network fetch and the
offline-first guarantee holds.
"""

from __future__ import annotations

SETTING_FAMILY = "ui.font_family"
SETTING_SIZE = "ui.font_size"
# Global bold: renders every text style of the chosen family in bold.
SETTING_BOLD = "ui.font_bold"
# Separate print typography, so screen and paper can differ. Both fall back to
# the UI values when unset (existing installs keep printing as before).
SETTING_PRINT_FAMILY = "print.font_family"
SETTING_PRINT_SIZE = "print.font_size"

DEFAULT_FAMILY = "auto"
DEFAULT_SIZE = 13
DEFAULT_BOLD = False

# (key, label). ``auto`` = the bundled app typeface when available.
FAMILIES: list[tuple[str, str]] = [
    ("auto", "الافتراضي (خط التطبيق)"),
    ("Segoe UI", "Segoe UI"),
    ("Tahoma", "Tahoma"),
    ("Arial", "Arial"),
    ("Calibri", "Calibri"),
    ("Verdana", "Verdana"),
    ("Georgia", "Georgia"),
    ("Candara", "Candara"),
    ("Corbel", "Corbel"),
    ("Times New Roman", "Times New Roman"),
    ("Courier New", "Courier New"),
    # Arabic-capable families that ship with Windows.
    ("Dubai", "Dubai"),
    ("Sakkal Majalla", "Sakkal Majalla"),
    ("Simplified Arabic", "Simplified Arabic"),
    ("Traditional Arabic", "Traditional Arabic"),
]
_FAMILY_LABELS = dict(FAMILIES)

# (base point size, label). The same base drives the UI theme and the printed
# document body so screen and paper stay in step.
SIZES: list[tuple[int, str]] = [
    (9, "صغير جدًا"),
    (11, "صغير"),
    (13, "عادي"),
    (15, "كبير"),
    (17, "كبير جدًا"),
    (19, "ضخم"),
]
_SIZE_VALUES = {px for px, _ in SIZES}
_SIZE_LABELS = dict(SIZES)


def family(settings) -> str:
    try:
        value = str(settings.get(SETTING_FAMILY, DEFAULT_FAMILY)).strip()
    except Exception:
        return DEFAULT_FAMILY
    return value if value in _FAMILY_LABELS else DEFAULT_FAMILY


def font_size(settings) -> int:
    try:
        value = int(float(settings.get(SETTING_SIZE, DEFAULT_SIZE)))
    except Exception:
        return DEFAULT_SIZE
    return value if value in _SIZE_VALUES else DEFAULT_SIZE


def ui_bold(settings) -> bool:
    """Whether the whole UI should render in a bold weight of the chosen font."""
    try:
        return str(settings.get(SETTING_BOLD, "1" if DEFAULT_BOLD else "0")).strip() == "1"
    except Exception:
        return DEFAULT_BOLD


def ui_scale_factor(settings) -> float:
    """Multiplier for on-screen text relative to the default size."""
    return font_size(settings) / float(DEFAULT_SIZE)


def ui_family(settings, bundled: str | None) -> str | None:
    """Family for ``page.theme``; ``None`` lets Flutter use its default."""
    chosen = family(settings)
    if chosen == "auto":
        return bundled
    return chosen


def print_family(settings, bundled: str | None) -> str | None:
    """Family to place first in the print CSS stack.

    Prefers the dedicated print family; falls back to the UI family, then the
    bundled default -- so splitting the settings never changes existing print
    output until the admin actually picks a print-specific family.
    """
    try:
        chosen = str(settings.get(SETTING_PRINT_FAMILY, "")).strip()
    except Exception:
        chosen = ""
    if chosen not in _FAMILY_LABELS:
        chosen = family(settings)
    if chosen == "auto":
        return bundled
    return chosen


def print_font_size(settings) -> int:
    """Point size for printed documents (print-specific, else UI size)."""
    try:
        value = int(float(settings.get(SETTING_PRINT_SIZE, "")))
    except (TypeError, ValueError):
        return font_size(settings)
    return value if value in _SIZE_VALUES else font_size(settings)


def label_for_family(key: str) -> str:
    return _FAMILY_LABELS.get(key, key)


def label_for_size(px: int) -> str:
    return _SIZE_LABELS.get(px, str(px))


__all__ = [
    "SETTING_FAMILY", "SETTING_SIZE", "SETTING_PRINT_FAMILY", "SETTING_PRINT_SIZE",
    "DEFAULT_FAMILY", "DEFAULT_SIZE",
    "FAMILIES", "SIZES", "family", "font_size", "ui_scale_factor", "ui_family",
    "print_family", "print_font_size",
    "label_for_family", "label_for_size",
]
