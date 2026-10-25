from __future__ import annotations

"""Business profile used by printed documents.

The profile is deliberately stored in the open-ended ``settings`` table (no
schema migration). The activity type is snapshotted onto every new invoice
together with the print options, so changing it later never relabels historical
invoices; contact details stay live so a corrected phone/address appears on all
documents.
"""

BUSINESS_TYPE_KEY = "business_type"
BUSINESS_PHONE_KEY = "business_phone"
BUSINESS_ADDRESS_KEY = "business_address"
BUSINESS_TAX_NUMBER_KEY = "business_tax_number"

DEFAULT_BUSINESS_TYPE = "retail"

# Labels are intentionally simple and Arabic-first. They change document
# wording, not the underlying accounting or table structure.
BUSINESS_TYPES: dict[str, dict[str, str]] = {
    "retail": {
        "label": "تجزئة",
        "sale_title": "فاتورة بيع",
        "purchase_title": "فاتورة شراء",
        "item_label": "المادة",
        "quantity_label": "الكمية",
        "customer_label": "العميل",
        "reference_label": "المرجع",
    },
    "wholesale": {
        "label": "جملة",
        "sale_title": "فاتورة بيع بالجملة",
        "purchase_title": "فاتورة شراء بالجملة",
        "item_label": "الصنف",
        "quantity_label": "الكمية",
        "customer_label": "العميل",
        "reference_label": "المرجع",
    },
    "restaurant": {
        "label": "مطعم / مقهى",
        "sale_title": "فاتورة طلب",
        "purchase_title": "فاتورة مشتريات",
        "item_label": "الصنف",
        "quantity_label": "العدد",
        "customer_label": "الزبون",
        "reference_label": "رقم الطلب / الطاولة",
    },
    "services": {
        "label": "خدمات",
        "sale_title": "فاتورة خدمات",
        "purchase_title": "فاتورة مشتريات",
        "item_label": "الخدمة",
        "quantity_label": "المدة",
        "customer_label": "العميل",
        "reference_label": "رقم المشروع",
    },
}


def normalize_type(value) -> str:
    """Return a known business type, falling back to retail."""
    key = str(value or "").strip()
    return key if key in BUSINESS_TYPES else DEFAULT_BUSINESS_TYPE


def business_type(settings) -> str:
    return normalize_type(settings.get(BUSINESS_TYPE_KEY))


def labels_for_type(value) -> dict[str, str]:
    return dict(BUSINESS_TYPES[normalize_type(value)])


def profile(settings) -> dict[str, str]:
    """Labels plus optional contact fields for the printed header."""
    data = labels_for_type(business_type(settings))
    data.update(
        {
            "business_type": business_type(settings),
            "phone": (settings.get(BUSINESS_PHONE_KEY) or "").strip(),
            "address": (settings.get(BUSINESS_ADDRESS_KEY) or "").strip(),
            "tax_number": (settings.get(BUSINESS_TAX_NUMBER_KEY) or "").strip(),
        }
    )
    return data


__all__ = [
    "BUSINESS_TYPE_KEY", "BUSINESS_PHONE_KEY", "BUSINESS_ADDRESS_KEY",
    "BUSINESS_TAX_NUMBER_KEY", "DEFAULT_BUSINESS_TYPE", "BUSINESS_TYPES",
    "normalize_type", "business_type", "labels_for_type", "profile",
]
