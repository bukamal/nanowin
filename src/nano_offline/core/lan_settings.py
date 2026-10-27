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

import ipaddress

ENABLED_KEY = "lan_price_server_enabled"
PORT_KEY = "lan_price_server_port"
HOST_KEY = "lan_price_server_host"
TITLE_KEY = "lan_kiosk_title"
ALLOWED_SUBNETS_KEY = "lan_allowed_subnets"
IDLE_SECONDS_KEY = "lan_kiosk_idle_seconds"
HTTPS_KEY = "lan_price_server_https"

DEFAULT_ENABLED = False
DEFAULT_PORT = 8770
DEFAULT_HOST = "0.0.0.0"
DEFAULT_TITLE = ""
DEFAULT_ALLOWED_SUBNETS = ""
DEFAULT_IDLE_SECONDS = 12
DEFAULT_HTTPS = False

MIN_PORT = 1024
MAX_PORT = 65535
MIN_IDLE_SECONDS = 3
MAX_IDLE_SECONDS = 120


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


def https_enabled(settings) -> bool:
    """Whether to serve the price kiosk over HTTPS (required for the camera).

    A self-signed certificate is generated on first use; the browser will warn
    until it is trusted on each device, but ``getUserMedia`` (camera) and the
    Wake Lock API only work in a secure context.
    """
    return _flag(settings, HTTPS_KEY, DEFAULT_HTTPS)


def idle_seconds(settings) -> int:
    try:
        value = int(str(settings.get(IDLE_SECONDS_KEY, str(DEFAULT_IDLE_SECONDS))).strip())
    except (TypeError, ValueError):
        return DEFAULT_IDLE_SECONDS
    return value if MIN_IDLE_SECONDS <= value <= MAX_IDLE_SECONDS else DEFAULT_IDLE_SECONDS


def allowed_networks(settings) -> tuple:
    """Parse the allow-list of client networks (comma/space separated CIDR/IP).

    Empty -> no restriction (any LAN host may connect). Invalid entries are
    ignored rather than failing the whole list.
    """
    raw = str(settings.get(ALLOWED_SUBNETS_KEY, DEFAULT_ALLOWED_SUBNETS) or "")
    nets = []
    for token in raw.replace(",", " ").split():
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def client_allowed(settings, host: str) -> bool:
    """Whether a client IP may use the server (empty allow-list == allow all)."""
    nets = allowed_networks(settings)
    if not nets:
        return True
    try:
        ip = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    return any(ip in net for net in nets)


__all__ = [
    "ENABLED_KEY", "PORT_KEY", "HOST_KEY", "TITLE_KEY", "ALLOWED_SUBNETS_KEY",
    "IDLE_SECONDS_KEY", "HTTPS_KEY",
    "DEFAULT_ENABLED", "DEFAULT_PORT", "DEFAULT_HOST", "DEFAULT_TITLE",
    "DEFAULT_ALLOWED_SUBNETS", "DEFAULT_IDLE_SECONDS", "DEFAULT_HTTPS",
    "MIN_PORT", "MAX_PORT", "MIN_IDLE_SECONDS", "MAX_IDLE_SECONDS",
    "enabled", "port", "host", "title", "idle_seconds", "https_enabled",
    "allowed_networks", "client_allowed",
]
