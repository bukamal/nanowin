"""Safe mode ("الوضع الآمن"): hide private balances from customers' eyes.

When safe mode is on, every *sensitive* figure -- cash box, net/profit, cost,
margin and party balances -- is rendered as a fixed mask (``••••``) instead of
a number, so a customer standing at the counter never reads the shop's books.
Selling prices stay visible (they are already printed on the invoice); only
costs, profits and balances are hidden.

Two layers decide the effective state:

* a persisted global toggle (``SAFE_MODE_KEY``) that a privileged user flips;
* a role rule: front-line roles (``sales``/``viewer``) are *always* hidden and
  can never reveal, regardless of the toggle.

This is a shoulder-surfing guard, not encryption: anyone whose role is allowed
to reveal can still read the real numbers.
"""

from __future__ import annotations

from nano_offline.core import currency

SAFE_MODE_KEY = "ui_safe_mode"
MASK_TEXT = "••••"

# Roles that may see the real numbers (and flip the toggle).
PRIVILEGED_ROLES = frozenset({"admin", "accountant"})
# Front-line roles that are always hidden and cannot reveal.
FRONT_ROLES = frozenset({"sales", "viewer"})


def _truthy(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "on", "yes", "نعم"}


def is_safe_mode(settings) -> bool:
    try:
        return _truthy(settings.get(SAFE_MODE_KEY, "0"))
    except Exception:
        return False


def set_safe_mode(settings, enabled: bool) -> None:
    payload = "1" if enabled else "0"
    try:
        settings.set(SAFE_MODE_KEY, payload)
    except Exception:
        try:
            settings.set_many({SAFE_MODE_KEY: payload})
        except Exception:
            pass


def role_of(ctx) -> str | None:
    try:
        session = ctx.auth.current()
    except Exception:
        return None
    return getattr(session, "role", None) if session is not None else None


def is_forced(role: str | None) -> bool:
    """Front-line roles are always in safe mode (cannot reveal)."""
    return str(role or "").strip().lower() in FRONT_ROLES


def can_reveal(role: str | None) -> bool:
    return str(role or "").strip().lower() in PRIVILEGED_ROLES


def effective_safe_mode(settings, role: str | None) -> bool:
    return is_forced(role) or is_safe_mode(settings)


def hidden(ctx) -> bool:
    """True when the current user must see masked financial figures."""
    return effective_safe_mode(getattr(ctx, "settings", None), role_of(ctx))


def money(value, settings=None, role=None) -> str:
    """Format ``value`` (stored USD) or return the mask in safe mode."""
    if effective_safe_mode(settings, role):
        return MASK_TEXT
    try:
        return currency.format_amount(value, settings)
    except Exception:
        return MASK_TEXT


def money_ctx(value, ctx) -> str:
    return money(value, getattr(ctx, "settings", None), role_of(ctx))


__all__ = [
    "SAFE_MODE_KEY",
    "MASK_TEXT",
    "PRIVILEGED_ROLES",
    "FRONT_ROLES",
    "is_safe_mode",
    "set_safe_mode",
    "role_of",
    "is_forced",
    "can_reveal",
    "effective_safe_mode",
    "hidden",
    "money",
    "money_ctx",
]
