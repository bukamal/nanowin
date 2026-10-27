from __future__ import annotations

"""Phase 4 smoke: client-mode context (network auth + remote data + hybrid settings).

Starts a read/write LAN API server and drives it through ``ClientContext`` the
way a terminal would: has_users, login, read data over the LAN, read shared
settings from the server, and keep device-local preferences in a local file.
"""

from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core.fsutil import tempdir
from nano_offline.core.local_prefs import LocalPrefs
from nano_offline.services.client_context import ClientContext
from nano_offline.services.lan_api_server import LanApiServer

PASSWORD = "password123"

with tempdir(prefix="qeid-client-") as td:
    ctx = AppContext.create(Path(td) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.settings.set("company_name", "متجر الخادم")
    ctx.items.create(name="شاي", barcode="6221031400099", selling_price=5.0, purchase_price=2.0, quantity=20)

    server = LanApiServer(ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        prefs = LocalPrefs(Path(td) / "client_prefs.json")
        prefs.set("theme_mode", "dark")
        client = ClientContext(base, prefs=prefs)

        # auth + data over the LAN
        assert client.auth.has_users() is True
        session = client.auth.login("admin", PASSWORD)
        assert session.role == "admin" and client.auth.current().username == "admin"
        assert client.auth.require("items").id == session.id

        rows = client.items.list(search="شاي")
        assert rows and rows[0]["name"] == "شاي", rows

        # shared settings come from the server; device-local ones stay local
        assert client.settings.get("company_name") == "متجر الخادم"
        assert client.settings.get("theme_mode") == "dark"

        # device-local write is local; a shared write is refused
        client.settings.set("print_layout_pos", "roll")
        assert prefs.get("print_layout_pos") == "roll"
        try:
            client.settings.set("company_name", "محاولة")
            raise AssertionError("shared settings must not be writable from the client")
        except PermissionError:
            pass
    finally:
        server.stop()

print("client_mode_smoke_test passed")
