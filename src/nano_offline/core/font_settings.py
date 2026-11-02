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

DEFAULT_FAMILY = "auto"
DEFAULT_SIZE = 13

# (key, label). ``auto`` = the bundled app typeface when available.
FAMILIES: list[tuple[str, str]] = [
    ("auto", "الافتراضي (خط التطبيق)"),
    ("Segoe UI", "Segoe UI"),
    ("Tahoma", "Tahoma"),
    ("Arial", "Arial"),
    ("Calibri", "Calibri"),
    ("Times New Roman", "Times New Roman"),
    ("Courier New", "Courier New"),
]
_FAMILY_LABELS = dict(FAMILIES)

# (base point size, label). The same base drives the UI theme and the printed
# document body so screen and paper stay in step.
SIZES: list[tuple[int, str]] = [
    (11, "صغير"),
    (13, "عادي"),
    (15, "كبير"),
    (17, "كبير جدًا"),
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


def ui_family(settings, bundled: str | None) -> str | None:
    """Family for ``page.theme``; ``None`` lets Flutter use its default."""
    chosen = family(settings)
    if chosen == "auto":
        return bundled
    return chosen


def print_family(settings, bundled: str | None) -> str | None:
    """Family to place first in the print CSS stack (``None`` = default)."""
    return ui_family(settings, bundled)


def label_for_family(key: str) -> str:
    return _FAMILY_LABELS.get(key, key)


def label_for_size(px: int) -> str:
    return _SIZE_LABELS.get(px, str(px))


__all__ = [
    "SETTING_FAMILY", "SETTING_SIZE", "DEFAULT_FAMILY", "DEFAULT_SIZE",
    "FAMILIES", "SIZES", "family", "font_size", "ui_family", "print_family",
    "label_for_family", "label_for_size",
]
