from __future__ import annotations

"""Phase 6 smoke: context selection (local vs client) and the client startup
contract (login/splash calls reachable on a hybrid context, default off).
"""

from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core import remote_settings
from nano_offline.core.fsutil import tempdir
from nano_offline.core.local_prefs import LocalPrefs
from nano_offline.services.auth_service import AuthService
from nano_offline.services.client_auth import ClientAuth
from nano_offline.services.hybrid_context import context_for
from nano_offline.services.lan_api_server import LanApiServer
from nano_offline.services.remote_client import RemoteService

PASSWORD = "password123"
ROOT = Path(__file__).resolve().parents[1]

# --- main.py / admin wiring contract ---------------------------------------
main_src = (ROOT / "src/main.py").read_text(encoding="utf-8")
admin_src = (ROOT / "src/nano_offline/views/admin_view.py").read_text(encoding="utf-8")
assert "context_for" in main_src
for needle in ["client_mode_switch", "remote_settings.MODE_KEY", "اختبار الاتصال"]:
    assert needle in admin_src, needle

with tempdir(prefix="qeid-client-start-") as td:
    server_ctx = AppContext.create(Path(td) / "server.db")
    server_ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    server_ctx.items.create(name="أرز", barcode="6221031400055", selling_price=6.0, purchase_price=3.0, quantity=15)

    server = LanApiServer(server_ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"

        # Client mode OFF -> a plain local context (unchanged behaviour).
        off_prefs = LocalPrefs(Path(td) / "off_prefs.json")
        local_ctx = context_for(Path(td) / "local.db", off_prefs)
        assert isinstance(local_ctx, AppContext)
        assert isinstance(local_ctx.auth, AuthService)
        assert not isinstance(local_ctx.items, RemoteService)

        # Client mode ON -> hybrid context routed to the server.
        prefs = LocalPrefs(Path(td) / "on_prefs.json")
        prefs.set_many({remote_settings.MODE_KEY: "1", remote_settings.SERVER_URL_KEY: base})
        ctx = context_for(Path(td) / "client.db", prefs)
        assert isinstance(ctx.items, RemoteService)
        assert isinstance(ctx.auth, ClientAuth)

        # Client startup/login contract (the calls main.py / login gate make).
        assert ctx.license.status().valid is False  # fresh local license, no crash
        assert ctx.auth.has_users() is True
        assert ctx.auth.restore_saved_session() is None
        assert ctx.auth.remembered_username() == ""
        assert ctx.auth.saved_login_enabled("admin") is False
        session = ctx.auth.login("admin", PASSWORD)
        assert session.role == "admin" and ctx.auth.current().can("items")
        assert ctx.items.list(search="أرز")[0]["name"] == "أرز"
    finally:
        server.stop()

print("client_startup_smoke_test passed")
