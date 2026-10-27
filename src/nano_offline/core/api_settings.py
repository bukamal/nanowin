from __future__ import annotations

"""Admin-configurable settings for the LAN API server (client/server data plane).

The LAN price-check server (``services.lan_price_server``) is the
customer-facing read surface; this is the *operator* data plane used by the
future client/server mode -- terminal "clients" talk to one central "server"
over the LAN. Phase 1 exposes read-only methods (see
``services.lan_api_server.READ_METHODS``); writes arrive in phase 2.

The client allow-list is shared with the price server (``lan_settings``) so a
shop configures "who may reach this machine" once.
"""

from nano_offline.core import lan_settings

ENABLED_KEY = "lan_api_server_enabled"
PORT_KEY = "lan_api_server_port"
HOST_KEY = "lan_api_server_host"
TOKEN_KEY = "lan_api_server_token"

DEFAULT_ENABLED = False
DEFAULT_PORT = 8760
DEFAULT_HOST = "0.0.0.0"
DEFAULT_TOKEN = ""

MIN_PORT = 1024
MAX_PORT = 65535


def _flag(settings, key: str, default: bool) -> bool:
    raw = settings.get(key, "1" if default else "0")
    return str(raw).strip() == "1"


def enabled(settings) -> bool:
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


def token(settings) -> str:
    return str(settings.get(TOKEN_KEY, DEFAULT_TOKEN) or "").strip()


def client_allowed(settings, host_ip: str) -> bool:
    """Same subnet allow-list policy as the price server (configured once)."""
    return lan_settings.client_allowed(settings, host_ip)


__all__ = [
    "ENABLED_KEY", "PORT_KEY", "HOST_KEY", "TOKEN_KEY",
    "DEFAULT_ENABLED", "DEFAULT_PORT", "DEFAULT_HOST", "DEFAULT_TOKEN",
    "MIN_PORT", "MAX_PORT",
    "enabled", "port", "host", "token", "client_allowed",
]
