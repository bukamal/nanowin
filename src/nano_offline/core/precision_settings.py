"""Admin-configurable decimal places for money and quantities.

Three independent knobs, each stored in the open-ended ``settings`` table:

- ``price``  -- unit prices (سعر الوحدة)
- ``qty``    -- quantities / counts (الكميات)
- ``total``  -- totals and cash amounts (الإجماليات)

Each is either ``"auto"`` (follow the display currency's own convention:
0 for SYP, 2 for USD -- the historical behaviour) or an explicit 0-4. Kept
free of any ``flet`` import so ``core/currency.py`` (screen + print) and the
admin view share one source of truth.
"""

from __future__ import annotations

SETTING_PRICE = "ui.price_decimals"
SETTING_QTY = "ui.qty_decimals"
SETTING_TOTAL = "ui.total_decimals"

AUTO = "auto"
MIN_DECIMALS = 0
MAX_DECIMALS = 4

# (value, label) for the admin dropdown; "" -> AUTO.
CHOICES: list[tuple[str, str]] = [
    (AUTO, "تلقائي (حسب العملة)"),
    ("0", "0"),
    ("1", "1"),
    ("2", "2"),
    ("3", "3"),
    ("4", "4"),
]

_FIELDS = {"price": SETTING_PRICE, "qty": SETTING_QTY, "total": SETTING_TOTAL}


def raw(settings, field: str) -> str:
    key = _FIELDS.get(field)
    if key is None:
        return AUTO
    try:
        value = str(settings.get(key, AUTO)).strip()
    except Exception:
        return AUTO
    return value or AUTO


def decimals(settings, field: str) -> int | None:
    """Explicit decimal count for ``field``; ``None`` means "auto"."""
    value = raw(settings, field)
    if value == AUTO:
        return None
    try:
        count = int(float(value))
    except (TypeError, ValueError):
        return None
    return count if MIN_DECIMALS <= count <= MAX_DECIMALS else None


__all__ = [
    "SETTING_PRICE", "SETTING_QTY", "SETTING_TOTAL",
    "AUTO", "MIN_DECIMALS", "MAX_DECIMALS", "CHOICES",
    "raw", "decimals",
]
