from __future__ import annotations

"""Centralized, admin-configurable settings for the point-of-sale screen.

Same pattern as ``core/barcode_settings.py`` and ``core/invoice_settings.py``:
values live in the open-ended ``settings`` key/value table, read fresh on
every POS session with sane defaults, and written only from the admin
"نقطة البيع" tab (views/admin_view.py).
"""

# --- Payment sheet -------------------------------------------------------#
AUTO_PRINT_DEFAULT_KEY = "pos_auto_print_default"

# --- Stock policy ---------------------------------------------------------#
ALLOW_NEGATIVE_STOCK_KEY = "pos_allow_negative_stock"
SHOW_STOCK_IN_CART_KEY = "pos_show_stock_in_cart"

# --- Keyboard shortcut ----------------------------------------------------#
F12_PRINT_ENABLED_KEY = "pos_f12_print_enabled"

# --- Quick cash buttons ----------------------------------------------------#
QUICK_CASH_COUNT_KEY = "pos_quick_cash_count"

DEFAULT_AUTO_PRINT = True
DEFAULT_ALLOW_NEGATIVE_STOCK = False
DEFAULT_SHOW_STOCK_IN_CART = False
DEFAULT_F12_PRINT_ENABLED = True
DEFAULT_QUICK_CASH_COUNT = 3

VALID_QUICK_CASH_COUNT = (2, 3, 4, 5)


def _flag(settings, key: str, default: bool) -> bool:
    raw = settings.get(key, "1" if default else "0")
    return str(raw).strip() == "1"


def auto_print_default(settings) -> bool:
    """Whether the \"طباعة تلقائية بعد الدفع\" switch starts on each time
    the payment sheet opens. Cashiers can still flip it per-sale -- this
    only sets the starting state, so a shop that always prints receipts
    doesn't need to remember to tap it on every single sale."""
    return _flag(settings, AUTO_PRINT_DEFAULT_KEY, DEFAULT_AUTO_PRINT)


def allow_negative_stock(settings) -> bool:
    """Whether quick sale may commit a quantity larger than the on-hand
    stock, leaving the item at a negative quantity. Defaults to off so
    existing shops keep the strict inventory guard until they opt in."""
    return _flag(settings, ALLOW_NEGATIVE_STOCK_KEY, DEFAULT_ALLOW_NEGATIVE_STOCK)


def show_stock_in_cart(settings) -> bool:
    """Whether cart lines show the remaining on-hand quantity. Hidden by
    default so the cashier's screen does not advertise stock levels; the
    admin can turn the hint back on for shops that rely on it."""
    return _flag(settings, SHOW_STOCK_IN_CART_KEY, DEFAULT_SHOW_STOCK_IN_CART)


def f12_print_enabled(settings) -> bool:
    """Whether the POS keyboard handler maps F12 to \"print last sale\"."""
    return _flag(settings, F12_PRINT_ENABLED_KEY, DEFAULT_F12_PRINT_ENABLED)


def quick_cash_count(settings) -> int:
    """How many rounded quick-cash suggestion buttons (beyond \"المبلغ
    بالتمام\") appear above the numpad. A busy counter shop typically wants
    fewer taps to scan past; a shop taking odd cash amounts benefits from
    more choices."""
    try:
        value = int(settings.get(QUICK_CASH_COUNT_KEY, str(DEFAULT_QUICK_CASH_COUNT)))
    except (TypeError, ValueError):
        return DEFAULT_QUICK_CASH_COUNT
    return value if value in VALID_QUICK_CASH_COUNT else DEFAULT_QUICK_CASH_COUNT


__all__ = [
    "AUTO_PRINT_DEFAULT_KEY", "ALLOW_NEGATIVE_STOCK_KEY", "SHOW_STOCK_IN_CART_KEY",
    "F12_PRINT_ENABLED_KEY", "QUICK_CASH_COUNT_KEY",
    "DEFAULT_AUTO_PRINT", "DEFAULT_ALLOW_NEGATIVE_STOCK", "DEFAULT_SHOW_STOCK_IN_CART",
    "DEFAULT_F12_PRINT_ENABLED", "DEFAULT_QUICK_CASH_COUNT", "VALID_QUICK_CASH_COUNT",
    "auto_print_default", "allow_negative_stock", "show_stock_in_cart",
    "f12_print_enabled", "quick_cash_count",
]
