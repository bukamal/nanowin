"""Restaurant mode: preparation orders before payment.

When enabled, a sale can be saved *unpaid* without picking a customer -- the
cashier rings the items, the kitchen tickets print immediately (station
printing already fires on sale creation), and the amount is settled later
through the normal payment flow. The open balance is attributed to a
dedicated "طلبات مفتوحة" customer so double-entry stays intact.

Also holds the restaurant POS behaviour switches (partial payment, tip,
order cancellation) so they are configured from Admin, not hardcoded.
"""

from __future__ import annotations

__all__ = [
    "RESTAURANT_MODE_KEY",
    "USE_TABLES_KEY",
    "ALLOW_PARTIAL_KEY",
    "ALLOW_TIP_KEY",
    "ALLOW_CANCEL_KEY",
    "CANCEL_PRINT_VOID_KEY",
    "DEFAULT_PARTY_NAME",
    "enabled",
    "set_enabled",
    "use_tables",
    "set_use_tables",
    "flag",
    "set_flag",
    "allow_partial",
    "allow_tip",
    "allow_cancel",
    "cancel_print_void",
]

RESTAURANT_MODE_KEY = "restaurant_mode"
#: Whether dine-in tables exist. Off = orders are just auto-numbered open
#: orders (quick service / takeaway) with no table picker anywhere.
USE_TABLES_KEY = "restaurant_use_tables"
#: Restaurant POS behaviour switches (defaults: partial/tip off, cancel on).
ALLOW_PARTIAL_KEY = "restaurant_allow_partial"
ALLOW_TIP_KEY = "restaurant_allow_tip"
ALLOW_CANCEL_KEY = "restaurant_allow_cancel"
CANCEL_PRINT_VOID_KEY = "restaurant_cancel_print_void"
DEFAULT_PARTY_NAME = "طلبات مفتوحة"

_TRUTHY = {"1", "true", "yes", "on"}
_DEFAULTS = {
    ALLOW_PARTIAL_KEY: False,
    ALLOW_TIP_KEY: False,
    ALLOW_CANCEL_KEY: True,
    CANCEL_PRINT_VOID_KEY: True,
}


def _raw(settings, key: str) -> str | None:
    try:
        if hasattr(settings, "get"):
            return str(settings.get(key, "") or "")
        return str((settings or {}).get(key, "") or "")
    except Exception:
        return None


def _flag(settings, key: str) -> bool:
    value = _raw(settings, key)
    if value is None:
        return bool(_DEFAULTS.get(key, False))
    text = value.strip().lower()
    if text == "":
        # A missing/empty value means "not configured yet" -> use the default.
        return bool(_DEFAULTS.get(key, False))
    return text in _TRUTHY


def flag(settings, key: str) -> bool:
    return _flag(settings, key)


def set_flag(settings_repo, key: str, value: bool) -> None:
    settings_repo.set(key, "1" if value else "0")


def enabled(settings) -> bool:
    """Accept a settings dict or a SettingsRepository-like object."""
    return _flag(settings, RESTAURANT_MODE_KEY)


def set_enabled(settings_repo, value: bool) -> None:
    settings_repo.set(RESTAURANT_MODE_KEY, "1" if value else "")


def use_tables(settings) -> bool:
    return _flag(settings, USE_TABLES_KEY)


def set_use_tables(settings_repo, value: bool) -> None:
    settings_repo.set(USE_TABLES_KEY, "1" if value else "")


def allow_partial(settings) -> bool:
    return _flag(settings, ALLOW_PARTIAL_KEY)


def allow_tip(settings) -> bool:
    return _flag(settings, ALLOW_TIP_KEY)


def allow_cancel(settings) -> bool:
    return _flag(settings, ALLOW_CANCEL_KEY)


def cancel_print_void(settings) -> bool:
    return _flag(settings, CANCEL_PRINT_VOID_KEY)