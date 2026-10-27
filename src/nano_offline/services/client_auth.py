from __future__ import annotations

"""Client-side authentication for the future client/server mode.

The desktop shell authenticates against a local ``AuthService``; a client
terminal authenticates against the central server instead. ``ClientAuth``
mirrors the subset of the ``AuthService`` surface the shell/login gate use
(``has_users``, ``login``, ``current``, ``require``, ``logout``) but talks to
the server over the LAN and keeps the resulting session in memory.

It shares the *same* :class:`RemoteTransport` as the client's data services, so
the session token obtained at login is attached to every subsequent read/write
automatically.

Full parity (saved/quick sessions, user administration) is a later step.
"""

import json
import urllib.request

from nano_offline.core.transport import RemoteTransport
from nano_offline.services.auth_service import UserSession


class ClientAuth:
    """Network-backed authentication for a client terminal."""

    def __init__(self, transport: RemoteTransport):
        self._transport = transport
        self._session: UserSession | None = None

    def has_users(self) -> bool:
        try:
            return bool(self._transport.call("auth", "has_users"))
        except Exception:
            return False

    def current(self) -> UserSession | None:
        return self._session

    # --- shell/login-gate parity (client sessions are not persisted) -----
    def restore_saved_session(self) -> UserSession | None:
        return None

    def remembered_username(self) -> str:
        return ""

    def saved_login_enabled(self, username: str | None = None) -> bool:
        return False

    def set_remembered_username(self, username: str | None) -> None:
        return None

    def quick_auth_info(self, username: str) -> None:
        return None

    def login_quick(self, *args, **kwargs):
        raise PermissionError("الدخول السريع غير مدعوم على العميل")

    def create_initial_admin(self, *args, **kwargs):
        raise PermissionError("أنشئ المدير الأول على الخادم، لا على العميل")

    # --- user administration (runs on the server as this admin) ----------
    def list_users(self) -> list:
        return self._transport.call("auth", "list_users")

    def create_user(self, **kwargs):
        return self._transport.call("auth", "create_user", **kwargs)

    def set_active(self, user_id: int, active: bool) -> None:
        return self._transport.call("auth", "set_active", user_id=int(user_id), active=bool(active))

    def reset_password(self, user_id: int, new_password: str) -> None:
        return self._transport.call(
            "auth", "reset_password", user_id=int(user_id), new_password=str(new_password)
        )

    def audit_entries(self, limit: int = 200) -> list:
        return self._transport.call("auth", "audit_entries", limit=int(limit))

    def require(self, permission: str) -> UserSession:
        session = self._session
        if session is None:
            raise PermissionError("يجب تسجيل الدخول")
        if not session.can(permission):
            raise PermissionError(f"لا تملك صلاحية: {permission}")
        return session

    def login(self, username: str, password: str, **_kwargs) -> UserSession:
        body = json.dumps({"username": username, "password": password}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._transport.token:
            headers["Authorization"] = f"Bearer {self._transport.token}"
        request = urllib.request.Request(  # noqa: S310 - caller-provided LAN URL
            self._transport.base_url + "/api/login", data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self._transport.timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ValueError("تعذر الاتصال بالخادم") from exc
        if not payload.get("ok"):
            raise ValueError(str(payload.get("error") or "فشل تسجيل الدخول"))
        user = payload.get("user") or {}
        # Store the token on the SHARED transport so data calls carry it too.
        self._transport.session = str(payload.get("session") or "")
        self._session = UserSession(
            int(user.get("id") or 0),
            str(user.get("username") or ""),
            str(user.get("full_name") or ""),
            str(user.get("role") or ""),
        )
        return self._session

    def logout(self) -> None:
        self._session = None
        self._transport.session = None


__all__ = ["ClientAuth"]
