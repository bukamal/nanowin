"""Restaurant mode: preparation orders before payment.

When enabled, a sale can be saved *unpaid* without picking a customer -- the
cashier rings the items, the kitchen tickets print immediately (station
printing already fires on sale creation), and the amount is settled later
through the normal payment flow. The open balance is attributed to a
dedicated "طلبات مفتوحة" customer so double-entry stays intact.
"""

from __future__ import annotations

__all__ = [
    "RESTAURANT_MODE_KEY",
    "USE_TABLES_KEY",
    "DEFAULT_PARTY_NAME",
    "enabled",
    "set_enabled",
    "use_tables",
    "set_use_tables",
]

RESTAURANT_MODE_KEY = "restaurant_mode"
#: Whether dine-in tables exist. Off = orders are just auto-numbered open
#: orders (quick service / takeaway) with no table picker anywhere.
USE_TABLES_KEY = "restaurant_use_tables"
DEFAULT_PARTY_NAME = "طلبات مفتوحة"

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(settings, key: str) -> bool:
    try:
        if hasattr(settings, "get"):
            value = settings.get(key, "")
        else:
            value = (settings or {}).get(key, "")
    except Exception:
        return False
    return str(value or "").strip().lower() in _TRUTHY


def enabled(settings) -> bool:
    """Accept a settings dict or a SettingsRepository-like object."""
    return _flag(settings, RESTAURANT_MODE_KEY)


def set_enabled(settings_repo, value: bool) -> None:
    settings_repo.set(RESTAURANT_MODE_KEY, "1" if value else "")


def use_tables(settings) -> bool:
    return _flag(settings, USE_TABLES_KEY)


def set_use_tables(settings_repo, value: bool) -> None:
    settings_repo.set(USE_TABLES_KEY, "1" if value else "")