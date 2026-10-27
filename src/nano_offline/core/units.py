from __future__ import annotations

"""Unit heuristics shared by the POS and the LAN price-check server.

There is no dedicated "sold by weight" flag on units/items, so a unit is
treated as a weight/volume unit when its name/abbreviation matches a common
weight/volume list. Deliberately a plain set match (not substring) against
the trimmed, case-folded text, so an unrelated unit that merely contains one
of these letters (e.g. "غرفة") never false-matches.
"""

WEIGHT_UNIT_HINTS = frozenset({
    "كغ", "كجم", "كغم", "كيلو", "كيلوغرام", "كيلوجرام",
    "غ", "غرام", "جرام",
    "طن",
    "لتر", "مل",
    "kg", "kgs", "g", "gram", "grams",
    "l", "liter", "litre", "ml",
})


def is_weighted_unit(*candidates: str) -> bool:
    """True when any of the given unit name/abbreviation texts is a weight/volume unit."""
    for value in candidates:
        text = str(value or "").strip().casefold()
        if text and text in WEIGHT_UNIT_HINTS:
            return True
    return False


__all__ = ["WEIGHT_UNIT_HINTS", "is_weighted_unit"]
