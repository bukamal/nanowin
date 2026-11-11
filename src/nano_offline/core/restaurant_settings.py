"""Restaurant mode: preparation orders before payment.

When enabled, a sale can be saved *unpaid* without picking a customer -- the
cashier rings the items, the kitchen tickets print immediately (station
printing already fires on sale creation), and the amount is settled later
through the normal payment flow. The open balance is attributed to a
dedicated "طلبات مفتوحة" customer so double-entry stays intact.
"""

from __future__ import annotations

__all__ = ["RESTAURANT_MODE_KEY", "DEFAULT_PARTY_NAME", "enabled", "set_enabled"]

RESTAURANT_MODE_KEY = "restaurant_mode"
DEFAULT_PARTY_NAME = "طلبات مفتوحة"

_TRUTHY = {"1", "true", "yes", "on"}


def enabled(settings) -> bool:
    """Accept a settings dict or a SettingsRepository-like object."""
    try:
        if hasattr(settings, "get"):
            value = settings.get(RESTAURANT_MODE_KEY, "")
        else:
            value = (settings or {}).get(RESTAURANT_MODE_KEY, "")
    except Exception:
        return False
    return str(value or "").strip().lower() in _TRUTHY


def set_enabled(settings_repo, value: bool) -> None:
    settings_repo.set(RESTAURANT_MODE_KEY, "1" if value else "")