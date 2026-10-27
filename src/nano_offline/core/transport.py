from __future__ import annotations

"""Generic, service-level transport seam for the future client/server mode.

Every data operation already flows through the ``AppContext`` services (each
service/repository is UI-free and owns its own SQL), so a call can be described
generically as ``(service, method, kwargs)``. ``LocalTransport`` dispatches such
a call to the real in-process services.

This is the *one contract* both sides of the future LAN client/server mode
agree on:

* the **server** routes an incoming RPC request through exactly this call
  shape (``LocalTransport`` or an equivalent dispatch), and
* the **client** implements the same :class:`Transport` protocol against HTTP,
  so ``AppContext``-style access keeps working unchanged.

Phase 0 ships only the local implementation -- no behaviour change; the remote
transport arrives together with the server.
"""

from typing import Any, Protocol, runtime_checkable

import json
import urllib.error
import urllib.request


class AttrDict(dict):
    """A ``dict`` whose keys are also readable as attributes.

    The server serialises dataclass results to dicts; wrapping them here lets
    client code keep using ``alert.title`` exactly as it did against the local
    service (item access still works too). Note: a key colliding with a real
    dict method (``items``/``keys``/``get``) is only reachable via ``[]``.
    """

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _wrap(value):
    if isinstance(value, dict):
        return AttrDict({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


@runtime_checkable
class Transport(Protocol):
    """Dispatch a service method call and return its (JSON-friendly) result."""

    def call(self, service: str, method: str, **kwargs: Any) -> Any: ...


class LocalTransport:
    """In-process dispatch to an ``AppContext``'s services."""

    def __init__(self, ctx: Any):
        self._ctx = ctx

    def call(self, service: str, method: str, **kwargs: Any) -> Any:
        target = getattr(self._ctx, service, None)
        if target is None:
            raise AttributeError(f"unknown service: {service!r}")
        func = getattr(target, method, None)
        if not callable(func):
            raise AttributeError(f"unknown method: {service}.{method}")
        return func(**kwargs)


class RemoteTransport:
    """HTTP client implementing :class:`Transport` against a LAN API server.

    Every call becomes ``POST {base_url}/api/rpc`` with a JSON body
    ``{"service", "method", "args"}``; the server dispatches to its real
    services and returns ``{"ok": true, "result": ...}``.
    """

    def __init__(self, base_url: str, *, token: str | None = None, session: str | None = None, timeout: float = 15.0):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = token or None
        self.session = session or None
        self.timeout = float(timeout)

    def call(self, service: str, method: str, **kwargs: Any) -> Any:
        body = json.dumps({"service": service, "method": method, "args": kwargs}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            headers["X-Session-Token"] = self.session
        request = urllib.request.Request(  # noqa: S310 - caller-provided LAN URL
            self.base_url + "/api/rpc", data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            raise RuntimeError(f"remote error {exc.code}: {detail or exc.reason}") from exc
        except Exception as exc:
            raise RuntimeError(f"remote call failed: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(str((payload or {}).get("error") or "remote call failed"))
        return _wrap(payload.get("result"))


__all__ = ["Transport", "LocalTransport", "RemoteTransport", "AttrDict"]

