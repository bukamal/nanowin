from __future__ import annotations

"""Centralized, admin-configurable settings for the invoice document system.

Same pattern as ``core/barcode_settings.py`` (see that module's docstring):
everything lives in the open-ended ``settings`` key/value table already used
for currency/branding/barcode, so no schema migration is needed and every
setting takes effect in its owning surface: invoice numbering in the invoice
register, editor, and printed/PDF invoice; payment-age thresholds in the
invoice register; and number/barcode/rate/progress/footer/verification
options in printed documents.

This module only knows about *reading* settings with sane defaults. The
admin "الفواتير" tab (views/admin_view.py) writes the invoice-specific keys;
the "الهوية" tab writes the business-profile keys used for document labels
and contact details. New invoices freeze these choices in
``invoices.print_options`` so reprints remain historically stable.
"""

import json
from datetime import date, datetime, timedelta

from nano_offline.core import business_profile

# --- Numbering ---------------------------------------------------------#
NUMBER_PREFIX_KEY = "invoice_number_prefix"
NUMBER_PADDING_KEY = "invoice_number_padding"

# --- Due date / overdue tracking ---------------------------------------#
DEFAULT_DUE_DAYS_KEY = "invoice_default_due_days"
OVERDUE_DAYS_KEY = "invoice_overdue_days"

# --- Printed document ----------------------------------------------------#
FOOTER_TEXT_KEY = "invoice_footer_text"
SHOW_NUMBER_KEY = "invoice_print_show_number"
SHOW_BARCODE_KEY = "invoice_print_show_barcode"
SHOW_EXCHANGE_RATE_KEY = "invoice_print_show_exchange_rate"
SHOW_PROGRESS_KEY = "invoice_print_show_progress"
SHOW_REMAINING_KEY = "invoice_print_show_remaining"
SHOW_UNIT_EQUIV_KEY = "invoice_print_show_unit_equiv"
SHOW_VERIFY_QR_KEY = "invoice_show_verify_qr"

# --- Stock policy ---------------------------------------------------------#
ALLOW_NEGATIVE_STOCK_KEY = "invoice_allow_negative_stock"
SHOW_STOCK_BADGE_KEY = "invoice_show_stock_badge"

DEFAULT_NUMBER_PREFIX = ""
DEFAULT_NUMBER_PADDING = 0
DEFAULT_DUE_DAYS = 0  # 0 == no due-date line printed (cash-style invoices)
DEFAULT_OVERDUE_DAYS = 30
DEFAULT_FOOTER_TEXT = ""
DEFAULT_SHOW_NUMBER = False
DEFAULT_SHOW_BARCODE = False
DEFAULT_SHOW_EXCHANGE_RATE = False
DEFAULT_SHOW_PROGRESS = False
DEFAULT_SHOW_REMAINING = False
DEFAULT_SHOW_UNIT_EQUIV = False
DEFAULT_SHOW_VERIFY_QR = False
DEFAULT_ALLOW_NEGATIVE_STOCK = False
DEFAULT_SHOW_STOCK_BADGE = True

VALID_NUMBER_PADDING = (0, 4, 5, 6)


def _flag(settings, key: str, default: bool) -> bool:
    raw = settings.get(key, "1" if default else "0")
    return str(raw).strip() == "1"


def number_prefix(settings) -> str:
    return (settings.get(NUMBER_PREFIX_KEY, DEFAULT_NUMBER_PREFIX) or "").strip()


def number_padding(settings) -> int:
    try:
        value = int(settings.get(NUMBER_PADDING_KEY, str(DEFAULT_NUMBER_PADDING)))
    except (TypeError, ValueError):
        return DEFAULT_NUMBER_PADDING
    return value if value in VALID_NUMBER_PADDING else DEFAULT_NUMBER_PADDING


def format_invoice_number(invoice_id: int, settings) -> str:
    """The display number printed/shown for an invoice id.

    The internal id (used for the barcode, the signature, and every DB
    join) never changes -- this only controls how it is *displayed*, so
    turning padding/prefix on or off is always safe and fully retroactive.
    """
    padding = number_padding(settings)
    digits = str(int(invoice_id)).zfill(padding) if padding else str(int(invoice_id))
    return f"{number_prefix(settings)}{digits}"


def default_due_days(settings) -> int:
    try:
        return max(0, int(settings.get(DEFAULT_DUE_DAYS_KEY, str(DEFAULT_DUE_DAYS))))
    except (TypeError, ValueError):
        return DEFAULT_DUE_DAYS


def overdue_days(settings) -> int:
    try:
        value = int(settings.get(OVERDUE_DAYS_KEY, str(DEFAULT_OVERDUE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_OVERDUE_DAYS
    return value if value > 0 else DEFAULT_OVERDUE_DAYS


def due_date_from_days(invoice_date_str: str | None, days) -> date | None:
    """Return the due date using an explicit due-days policy.

    Used by printed invoices so the policy snapshotted at creation wins over
    the admin's current setting.
    """
    try:
        days = max(0, int(days))
    except (TypeError, ValueError):
        return None
    if days <= 0 or not invoice_date_str:
        return None
    try:
        issued = datetime.fromisoformat(str(invoice_date_str)[:10]).date()
    except ValueError:
        return None
    return issued + timedelta(days=days)


def due_date_for(invoice_date_str: str | None, settings) -> date | None:
    """Return the due date for a credit invoice, or ``None`` when the admin
    hasn't set a default due-days window (cash-only shops leave this at 0
    and never see a due-date line on their invoices)."""
    return due_date_from_days(invoice_date_str, default_due_days(settings))


def footer_text(settings) -> str:
    return (settings.get(FOOTER_TEXT_KEY, DEFAULT_FOOTER_TEXT) or "").strip()


def show_invoice_number(settings) -> bool:
    """Whether the printed invoice shows its human-readable number."""
    return _flag(settings, SHOW_NUMBER_KEY, DEFAULT_SHOW_NUMBER)


def show_invoice_barcode(settings) -> bool:
    """Whether the printed invoice shows a Code128 of the internal id."""
    return _flag(settings, SHOW_BARCODE_KEY, DEFAULT_SHOW_BARCODE)


def show_exchange_rate(settings) -> bool:
    """Whether to print the historical FX rate and USD equivalents."""
    return _flag(settings, SHOW_EXCHANGE_RATE_KEY, DEFAULT_SHOW_EXCHANGE_RATE)


def show_payment_progress(settings) -> bool:
    """Whether to print the paid-percentage progress bar."""
    return _flag(settings, SHOW_PROGRESS_KEY, DEFAULT_SHOW_PROGRESS)


def show_remaining(settings) -> bool:
    """Whether the printed document shows the outstanding (remaining) line.

    Mirrors :func:`show_payment_progress`: hidden by default so a fully paid
    cash sale no longer prints a trailing "المتبقي 0.00" row, and opt-in from
    the admin "الفواتير" tab when credit invoices need it. The choice is
    snapshotted onto each invoice, so historical reprints stay stable.
    """
    return _flag(settings, SHOW_REMAINING_KEY, DEFAULT_SHOW_REMAINING)


def show_unit_equiv(settings) -> bool:
    """Whether printed lines show the cross-unit equivalent.

    e.g. a 10-metre line printing "≈ 0.5 كرتونة" underneath its quantity.
    Off by default so a plain receipt stays uncluttered; opt in from the
    admin "الفواتير" tab. Snapshotted per invoice like the other flags.
    """
    return _flag(settings, SHOW_UNIT_EQUIV_KEY, DEFAULT_SHOW_UNIT_EQUIV)


def show_verify_qr(settings) -> bool:
    return _flag(settings, SHOW_VERIFY_QR_KEY, DEFAULT_SHOW_VERIFY_QR)


def allow_negative_stock(settings) -> bool:
    """Whether saving a sale invoice may leave an item below zero.

    The permission is snapshotted onto each invoice, so turning this off
    later never invalidates historical oversold invoices during a rebuild.
    """
    return _flag(settings, ALLOW_NEGATIVE_STOCK_KEY, DEFAULT_ALLOW_NEGATIVE_STOCK)


def show_stock_badge(settings) -> bool:
    """Whether the invoice editor shows its low/out-of-stock hint."""
    return _flag(settings, SHOW_STOCK_BADGE_KEY, DEFAULT_SHOW_STOCK_BADGE)


PRINT_SNAPSHOT_KEYS = (
    "show_number",
    "show_barcode",
    "show_exchange_rate",
    "show_progress",
    "show_remaining",
    "show_unit_equiv",
    "show_verify_qr",
    "footer_text",
    "due_days",
    "business_type",
)


def print_snapshot_from_settings(settings) -> dict:
    """The print-related choices that a new invoice should freeze."""
    return {
        "show_number": show_invoice_number(settings),
        "show_barcode": show_invoice_barcode(settings),
        "show_exchange_rate": show_exchange_rate(settings),
        "show_progress": show_payment_progress(settings),
        "show_remaining": show_remaining(settings),
        "show_unit_equiv": show_unit_equiv(settings),
        "show_verify_qr": show_verify_qr(settings),
        "footer_text": footer_text(settings),
        "due_days": default_due_days(settings),
        "business_type": business_profile.business_type(settings),
    }


def encode_print_snapshot(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_print_snapshot(raw) -> dict | None:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _snapshot_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def print_snapshot(inv: dict | None, settings) -> dict:
    """Return the invoice's frozen print options, with a live-settings fallback.

    Invoices created before the snapshot column existed (or malformed rows)
    keep behaving exactly as before. Missing keys in an older snapshot are
    filled from the current settings, which makes future additions
    forward-compatible.
    """
    current = print_snapshot_from_settings(settings)
    saved = decode_print_snapshot((inv or {}).get("print_options"))
    if not saved:
        return current
    merged = dict(current)
    for key in PRINT_SNAPSHOT_KEYS:
        if key in saved:
            merged[key] = saved[key]
    for key in ("show_number", "show_barcode", "show_exchange_rate", "show_progress", "show_remaining", "show_unit_equiv", "show_verify_qr"):
        merged[key] = _snapshot_bool(merged.get(key), current[key])
    try:
        merged["due_days"] = max(0, int(merged.get("due_days")))
    except (TypeError, ValueError):
        merged["due_days"] = current["due_days"]
    merged["footer_text"] = str(merged.get("footer_text") or "").strip()
    merged["business_type"] = business_profile.normalize_type(merged.get("business_type"))
    return merged


__all__ = [
    "NUMBER_PREFIX_KEY", "NUMBER_PADDING_KEY", "DEFAULT_DUE_DAYS_KEY",
    "OVERDUE_DAYS_KEY", "FOOTER_TEXT_KEY",
    "SHOW_NUMBER_KEY", "SHOW_BARCODE_KEY", "SHOW_EXCHANGE_RATE_KEY",
    "SHOW_PROGRESS_KEY", "SHOW_REMAINING_KEY", "SHOW_UNIT_EQUIV_KEY",
    "SHOW_VERIFY_QR_KEY",
    "ALLOW_NEGATIVE_STOCK_KEY", "SHOW_STOCK_BADGE_KEY",
    "DEFAULT_NUMBER_PREFIX", "DEFAULT_NUMBER_PADDING", "DEFAULT_DUE_DAYS",
    "DEFAULT_OVERDUE_DAYS", "DEFAULT_FOOTER_TEXT",
    "DEFAULT_SHOW_NUMBER", "DEFAULT_SHOW_BARCODE",
    "DEFAULT_SHOW_EXCHANGE_RATE", "DEFAULT_SHOW_PROGRESS",
    "DEFAULT_SHOW_REMAINING", "DEFAULT_SHOW_UNIT_EQUIV",
    "DEFAULT_SHOW_VERIFY_QR",
    "DEFAULT_ALLOW_NEGATIVE_STOCK", "DEFAULT_SHOW_STOCK_BADGE",
    "VALID_NUMBER_PADDING",
    "number_prefix", "number_padding", "format_invoice_number",
    "default_due_days", "overdue_days", "due_date_for", "due_date_from_days",
    "footer_text", "show_invoice_number", "show_invoice_barcode",
    "show_exchange_rate", "show_payment_progress", "show_remaining",
    "show_unit_equiv", "show_verify_qr", "allow_negative_stock",
    "show_stock_badge",
    "PRINT_SNAPSHOT_KEYS", "print_snapshot_from_settings",
    "encode_print_snapshot", "decode_print_snapshot", "print_snapshot",
]
