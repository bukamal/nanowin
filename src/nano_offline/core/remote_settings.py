from __future__ import annotations

"""Client-mode settings: point a terminal at a central LAN server.

A terminal can run as a "client" that talks to one central "server" (see
``services.lan_api_server``) instead of its own local database. Phase 3 lays
the configuration + a connection probe; the full UI switch to client mode is a
later step. Keys live in the existing ``settings`` table (device-local in
spirit -- see ``core.local_prefs``).
"""

MODE_KEY = "client_mode_enabled"
SERVER_URL_KEY = "client_server_url"
SERVER_TOKEN_KEY = "client_server_token"

DEFAULT_MODE = False
DEFAULT_SERVER_URL = ""
DEFAULT_SERVER_TOKEN = ""


def _flag(settings, key: str, default: bool) -> bool:
    raw = settings.get(key, "1" if default else "0")
    return str(raw).strip() == "1"


def client_mode(settings) -> bool:
    return _flag(settings, MODE_KEY, DEFAULT_MODE)


def normalize_url(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def server_url(settings) -> str:
    return normalize_url(settings.get(SERVER_URL_KEY, DEFAULT_SERVER_URL) or "")


def server_token(settings) -> str:
    return str(settings.get(SERVER_TOKEN_KEY, DEFAULT_SERVER_TOKEN) or "").strip()


__all__ = [
    "MODE_KEY", "SERVER_URL_KEY", "SERVER_TOKEN_KEY",
    "DEFAULT_MODE", "DEFAULT_SERVER_URL", "DEFAULT_SERVER_TOKEN",
    "client_mode", "normalize_url", "server_url", "server_token",
]
