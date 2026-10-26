"""Central user-visible brand strings.

Internal identifiers (the Python package, the Android org id, and the
Windows product folder) deliberately stay ``nano``/``com.nano`` so existing
installations keep updating in place without losing their local database.
Everything a user reads comes from here, so a future rename is one edit.
"""

from __future__ import annotations

APP_NAME_AR = "كاشير"
APP_NAME_EN = "Cashir"
APP_BRAND = "Cashir | كاشير"
APP_TAGLINE = "نظام نقاط البيع وإدارة المبيعات"
APP_DESCRIPTION = "نظام نقاط البيع وإدارة المبيعات — محاسبة ومخزون محلي"

DEFAULT_COMPANY_NAME = APP_NAME_AR

__all__ = [
    "APP_NAME_AR",
    "APP_NAME_EN",
    "APP_BRAND",
    "APP_TAGLINE",
    "APP_DESCRIPTION",
    "DEFAULT_COMPANY_NAME",
]
