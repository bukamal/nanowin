from __future__ import annotations

"""Phase 5 smoke: hybrid client context (local shell + remote business data).

Proves that a client terminal can keep local shell services while its business
data (items/invoices/...) is served from the central server, that the login
session is shared with the data transport (so remote writes are authorised),
and that shared vs device-local settings route correctly.
"""

from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core.fsutil import tempdir
from nano_offline.core.local_prefs import LocalPrefs
from nano_offline.services.hybrid_context import build_hybrid_context
from nano_offline.services.lan_api_server import LanApiServer
from nano_offline.services.remote_client import RemoteService

PASSWORD = "password123"

with tempdir(prefix="qeid-hybrid-") as td:
    server_ctx = AppContext.create(Path(td) / "server.db")
    server_ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    server_ctx.settings.set("company_name", "متجر الخادم")
    server_ctx.items.create(name="قهوة", barcode="6221031400077", selling_price=8.0, purchase_price=3.0, quantity=30)

    server = LanApiServer(server_ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        prefs = LocalPrefs(Path(td) / "client_prefs.json")
        prefs.set("theme_mode", "light")

        ctx = build_hybrid_context(Path(td) / "client.db", base, prefs=prefs)

        # Business services are remote; shell services stay local.
        assert isinstance(ctx.items, RemoteService)
        assert isinstance(ctx.invoices, RemoteService)
        assert not isinstance(ctx.license, RemoteService)
        assert not isinstance(ctx.backup, RemoteService)

        # Login over the LAN; the session is shared with the data transport.
        assert ctx.auth.has_users() is True
        ctx.auth.login("admin", PASSWORD)
        assert ctx.auth.current().role == "admin"

        rows = ctx.items.list(search="قهوة")
        assert rows and rows[0]["name"] == "قهوة", rows

        # A remote write works because the shared transport carries the session.
        new_id = ctx.items.create(name="سكر", selling_price=4.0, purchase_price=2.0)
        assert isinstance(new_id, int), new_id

        # Settings: shared from the server, device-local from the file.
        assert ctx.settings.get("company_name") == "متجر الخادم"
        assert ctx.settings.get("theme_mode") == "light"
        try:
            ctx.settings.set("company_name", "x")
            raise AssertionError("shared settings must not be writable from a client")
        except PermissionError:
            pass
    finally:
        server.stop()

print("hybrid_context_smoke_test passed")
