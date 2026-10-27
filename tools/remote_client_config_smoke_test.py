from __future__ import annotations

"""Phase 3 smoke: client-mode config, device-local preference store, the admin
"الخادم والعميل" surface, and remote_client.probe() against a live server.
"""

from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core import remote_settings
from nano_offline.core.fsutil import tempdir
from nano_offline.core.local_prefs import LocalPrefs, is_device_local
from nano_offline.services.lan_api_server import LanApiServer
from nano_offline.services.remote_client import probe

ROOT = Path(__file__).resolve().parents[1]

# --- remote_settings parsing ------------------------------------------------
assert remote_settings.normalize_url("cashir.local:8760") == "http://cashir.local:8760"
assert remote_settings.normalize_url("http://host/") == "http://host"
assert remote_settings.normalize_url("") == ""
assert remote_settings.client_mode({}) is False
assert remote_settings.client_mode({remote_settings.MODE_KEY: "1"}) is True
assert remote_settings.server_url({remote_settings.SERVER_URL_KEY: "192.168.1.10:8760"}) == "http://192.168.1.10:8760"
assert remote_settings.server_token({remote_settings.SERVER_TOKEN_KEY: " abc "}) == "abc"

# --- device-local classification --------------------------------------------
assert is_device_local("theme_mode") is True
assert is_device_local("print_layout_pos") is True
assert is_device_local("ui.dashboard_kpis_visible.7") is True
assert is_device_local("company_name") is False
assert is_device_local("invoice_print_show_remaining") is False

# --- device-local prefs store -----------------------------------------------
with tempdir(prefix="qeid-prefs-") as td:
    prefs = LocalPrefs(Path(td) / "local_prefs.json")
    prefs.set_many({"theme_mode": "dark", "print_layout": "roll"})
    assert prefs.get("theme_mode") == "dark"
    assert prefs.get_all()["print_layout"] == "roll"
    prefs.set("theme_mode", "")
    assert prefs.get("theme_mode") == ""

# --- admin "الخادم والعميل" surface (contract tokens) -----------------------
admin = (ROOT / "src/nano_offline/views/admin_view.py").read_text(encoding="utf-8")
for needle in ["الخادم والعميل", "api_settings", "remote_settings", "lan_api_server", "اختبار الاتصال"]:
    assert needle in admin, needle

# --- probe() against a live server ------------------------------------------
with tempdir(prefix="qeid-probe-") as td:
    ctx = AppContext.create(Path(td) / "nano.db")
    server = LanApiServer(ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        info = probe(f"http://127.0.0.1:{server.port}")
        assert info.get("ok") is True and info.get("app") == "cashir-lan-api", info
    finally:
        server.stop()

print("remote_client_config_smoke_test passed")
