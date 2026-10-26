from __future__ import annotations

"""Admin-configurable settings for the built-in LAN price-check server.

The desktop app can host a small read-only HTTP server
(``services.lan_price_server``) so customer price-check screens on the shop
LAN open a light web page that answers a barcode with the current price.

Everything lives in the existing open-ended ``settings`` table (same pattern
as ``barcode_settings``/``pos_settings``), so no schema migration is needed.
The admin "شاشة الأسعار" tab (views/admin_view.py) writes these keys; the
server reads them on start/restart.
"""

ENABLED_KEY = "lan_price_server_enabled"
PORT_KEY = "lan_price_server_port"
HOST_KEY = "lan_price_server_host"
TITLE_KEY = "lan_kiosk_title"

DEFAULT_ENABLED = False
DEFAULT_PORT = 8770
DEFAULT_HOST = "0.0.0.0"
DEFAULT_TITLE = ""

MIN_PORT = 1024
MAX_PORT = 65535


def _flag(settings, key: str, default: bool) -> bool:
    raw = settings.get(key, "1" if default else "0")
    return str(raw).strip() == "1"


def enabled(settings) -> bool:
    """Whether the LAN price-check server should run."""
    return _flag(settings, ENABLED_KEY, DEFAULT_ENABLED)


def port(settings) -> int:
    try:
        value = int(str(settings.get(PORT_KEY, str(DEFAULT_PORT))).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return value if MIN_PORT <= value <= MAX_PORT else DEFAULT_PORT


def host(settings) -> str:
    value = str(settings.get(HOST_KEY, DEFAULT_HOST) or "").strip()
    return value or DEFAULT_HOST


def title(settings, fallback: str = "") -> str:
    value = str(settings.get(TITLE_KEY, DEFAULT_TITLE) or "").strip()
    return value or (fallback or "")


__all__ = [
    "ENABLED_KEY", "PORT_KEY", "HOST_KEY", "TITLE_KEY",
    "DEFAULT_ENABLED", "DEFAULT_PORT", "DEFAULT_HOST", "DEFAULT_TITLE",
    "MIN_PORT", "MAX_PORT",
    "enabled", "port", "host", "title",
]
