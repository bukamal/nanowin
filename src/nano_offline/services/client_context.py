from __future__ import annotations

"""A client-mode stand-in for ``AppContext`` (client/server mode -- phase 4).

Exposes the shared, allow-listed business services as remote proxies, a network
``auth`` (:class:`ClientAuth`), and a ``settings`` wrapper that reads shared
business settings from the server while keeping *device-local* preferences
(theme, print profile, layout...) in a local file.

Phase 4 delivers the data + auth foundation. Wiring the whole desktop UI into
client mode is deferred: some services (license, notifications, backup) have no
server parity yet, so the flag stays off by default and local mode is unchanged.
"""

from nano_offline.core.local_prefs import LocalPrefs, is_device_local
from nano_offline.core.transport import RemoteTransport
from nano_offline.services.client_auth import ClientAuth
from nano_offline.services.remote_client import RemoteService


class ClientSettings:
    """Reads shared settings from the server; stores device-local keys locally."""

    def __init__(self, transport: RemoteTransport, prefs: LocalPrefs):
        self._transport = transport
        self._prefs = prefs
        self._cache: dict = {}

    def get(self, key: str, default: str = "") -> str:
        if is_device_local(key):
            return self._prefs.get(key, default)
        if key in self._cache:
            return self._cache[key]
        try:
            value = self._transport.call("settings", "get", key=str(key), default=default)
        except Exception:
            value = default
        self._cache[str(key)] = value
        return value

    def get_all(self) -> dict:
        data: dict = {}
        try:
            data.update(self._transport.call("settings", "get_all") or {})
        except Exception:
            pass
        data.update(self._prefs.get_all())
        return data

    def set(self, key: str, value) -> None:
        if not is_device_local(key):
            raise PermissionError("الإعدادات المشتركة تُعدّل على الخادم، لا على العميل")
        self._prefs.set(key, value)

    def set_many(self, values: dict) -> None:
        shared = [k for k in values if not is_device_local(k)]
        if shared:
            raise PermissionError("بعض الإعدادات مشتركة وتُعدّل على الخادم")
        self._prefs.set_many(values)


class ClientContext:
    """A minimal AppContext-compatible object whose data lives on the server."""

    def __init__(self, base_url: str, *, token: str | None = None, prefs: LocalPrefs | None = None):
        self._transport = RemoteTransport(base_url, token=token)
        self._base_url = base_url
        self.auth = ClientAuth(self._transport)
        self.settings = ClientSettings(self._transport, prefs or LocalPrefs("local_prefs.json"))

    def __getattr__(self, name: str) -> RemoteService:
        if name.startswith("_"):
            raise AttributeError(name)
        return RemoteService(self._transport, name)


__all__ = ["ClientSettings", "ClientContext"]
