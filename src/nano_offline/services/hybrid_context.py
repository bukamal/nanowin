from __future__ import annotations

"""Hybrid client context: local shell + remote business data (phase 5).

A client terminal keeps a *local* AppContext for shell concerns that have no
server parity yet (license, backups, native notifications) but replaces the
shared business services (items / invoices / customers / suppliers / reports /
documents / dashboard ...) with LAN proxies, plus a network ``auth`` and a
hybrid ``settings`` (shared keys read from the server, device-local keys kept
locally).

This is the structural core of client mode: the views keep calling
``ctx.items.list(...)`` etc. unchanged. Selecting this context from ``main.py``
is the final wiring step, gated behind the client-mode flag and a user login.
"""

from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core.local_prefs import LocalPrefs
from nano_offline.core.logging_config import get_logger
from nano_offline.core.transport import RemoteTransport
from nano_offline.services.client_auth import ClientAuth
from nano_offline.services.client_context import ClientSettings
from nano_offline.services.remote_client import RemoteService

log = get_logger("nano.lan.client")

#: Set by :func:`context_for` when client mode was requested but the app fell
#: back to local (server unreachable / setup failed). ``main.py`` surfaces it
#: as a toast so the user knows why they are running locally.
LAST_FALLBACK: str | None = None

#: Business services served from the central server (their allow-listed methods
#: form the shared data plane). Everything else on the AppContext stays local.
REMOTE_SERVICES = (
    "items", "invoices", "payments", "expenses", "customers", "suppliers",
    "definitions", "statements", "reports", "dashboard", "documents", "forecast",
    "notifications", "smart_assistant", "owner_pulse",
    "stocktake", "cash_day_close", "party_trust", "external_notifications",
)


def build_hybrid_context(
    local_db_path: str | Path,
    base_url: str,
    *,
    token: str | None = None,
    prefs: LocalPrefs | None = None,
) -> AppContext:
    """A local AppContext whose shared business services point at the server.

    The local database backs only the shell services (license/backup/native) and
    the device-local preference file; business data is remote.
    """
    path = Path(local_db_path)
    ctx = AppContext.create(path)
    transport = RemoteTransport(base_url, token=token)
    for name in REMOTE_SERVICES:
        setattr(ctx, name, RemoteService(transport, name))
    ctx.auth = ClientAuth(transport)
    ctx.settings = ClientSettings(transport, prefs or LocalPrefs(path.parent / "local_prefs.json"))
    return ctx


def context_for(db_path: str | Path, prefs: LocalPrefs | None = None) -> AppContext:
    """Build the context for this device: client (hybrid) when it opted in, else local.

    ``main.py`` calls this at startup. Client mode is off by default, so a normal
    install always gets the plain local context (identical behaviour).

    Fail-safe: if client mode is on but the central server is unreachable (or
    the client context can't be built), the app falls back to a local context
    instead of hanging on blocking network calls -- it never shows a blank
    screen. :data:`LAST_FALLBACK` records why, for the startup toast.
    """
    global LAST_FALLBACK
    LAST_FALLBACK = None

    from nano_offline.core import remote_settings
    from nano_offline.services.remote_client import probe

    path = Path(db_path)
    prefs = prefs or LocalPrefs(path.parent / "local_prefs.json")
    url = remote_settings.server_url(prefs)
    if remote_settings.client_mode(prefs) and url:
        token = remote_settings.server_token(prefs) or None
        try:
            probe(url, token=token, timeout=2.5)
        except Exception:
            LAST_FALLBACK = "تعذّر الوصول لخادم المتجر — تم التشغيل محليًا."
            log.warning("client mode: server unreachable at %s; using local context", url)
            return AppContext.create(path)
        try:
            return build_hybrid_context(path, url, token=token, prefs=prefs)
        except Exception:
            LAST_FALLBACK = "تعذّر تهيئة وضع العميل — تم التشغيل محليًا."
            log.warning("client mode: setup failed; using local context", exc_info=True)
            return AppContext.create(path)
    return AppContext.create(path)


__all__ = ["REMOTE_SERVICES", "build_hybrid_context", "context_for"]
