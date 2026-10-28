from __future__ import annotations

"""Client-side proxies for the future client/server mode.

``RemoteContext`` mirrors an ``AppContext``'s service attributes, but each method
call is forwarded over the LAN to the server's API (``core.transport.
RemoteTransport``). Views keep calling ``ctx.items.list(...)`` unchanged; only
the *context* is built differently (local today, remote once the client mode is
enabled in phase 3).
"""

from typing import Any

import asyncio

from nano_offline.core.transport import RemoteTransport


class _RemoteAio:
    """``service.aio.method(...)`` -> coroutine, off-loading the blocking HTTP
    call to a worker thread so the Flet event loop never freezes."""

    def __init__(self, transport: RemoteTransport, name: str):
        self._transport = transport
        self._name = name

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            if args:
                raise TypeError(f"{self._name}.aio.{method}() takes keyword arguments only")
            return await asyncio.to_thread(self._transport.call, self._name, method, **kwargs)

        return _call


class RemoteService:
    """Attribute access on this object yields a remote method caller."""

    def __init__(self, transport: RemoteTransport, name: str):
        self._transport = transport
        self._name = name

    @property
    def aio(self) -> _RemoteAio:
        return _RemoteAio(self._transport, self._name)

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)

        def _call(*args: Any, **kwargs: Any) -> Any:
            if args:
                raise TypeError(
                    f"{self._name}.{method}() must be called with keyword arguments over the LAN"
                )
            return self._transport.call(self._name, method, **kwargs)

        return _call


class RemoteContext:
    """A lightweight stand-in for ``AppContext`` whose services are remote."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 6.0):
        self.transport = RemoteTransport(base_url, token=token, timeout=timeout)

    def service(self, name: str) -> RemoteService:
        return RemoteService(self.transport, name)

    def login(self, username: str, password: str) -> dict:
        """Authenticate against the server; stores the session on the transport.

        Required before any write call (writes are rejected without a session).
        """
        import json
        import urllib.request

        body = json.dumps({"username": username, "password": password}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.transport.token:
            headers["Authorization"] = f"Bearer {self.transport.token}"
        request = urllib.request.Request(  # noqa: S310 - caller-provided LAN URL
            self.transport.base_url + "/api/login", data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.transport.timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"login failed: {exc}") from exc
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "login failed"))
        self.transport.session = str(payload.get("session") or "")
        return dict(payload.get("user") or {})

    def logout(self) -> None:
        if not self.transport.session:
            return
        import urllib.request

        headers = {"X-Session-Token": self.transport.session}
        if self.transport.token:
            headers["Authorization"] = f"Bearer {self.transport.token}"
        request = urllib.request.Request(  # noqa: S310
            self.transport.base_url + "/api/logout", data=b"{}", method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.transport.timeout):  # noqa: S310
                pass
        except Exception:
            pass
        self.transport.session = None

    def __getattr__(self, name: str) -> RemoteService:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.service(name)


def probe(base_url: str, *, token: str | None = None, timeout: float = 5.0) -> dict:
    """GET ``/api/health`` on a LAN server; returns the parsed JSON.

    Used by the admin "اختبار الاتصال" button. Raises on any failure so the
    caller can surface the reason.
    """
    import json
    import urllib.request

    url = str(base_url or "").rstrip("/") + "/api/health"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - caller-provided LAN URL
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


__all__ = ["RemoteService", "RemoteContext", "probe"]
