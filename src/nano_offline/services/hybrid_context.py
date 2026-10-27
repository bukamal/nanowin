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
from nano_offline.core.transport import RemoteTransport
from nano_offline.services.client_auth import ClientAuth
from nano_offline.services.client_context import ClientSettings
from nano_offline.services.remote_client import RemoteService

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
    """
    from nano_offline.core import remote_settings

    path = Path(db_path)
    prefs = prefs or LocalPrefs(path.parent / "local_prefs.json")
    url = remote_settings.server_url(prefs)
    if remote_settings.client_mode(prefs) and url:
        return build_hybrid_context(path, url, token=remote_settings.server_token(prefs) or None, prefs=prefs)
    return AppContext.create(path)


__all__ = ["REMOTE_SERVICES", "build_hybrid_context", "context_for"]
