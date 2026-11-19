from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.services.lan_api_server import LanApiServer
from nano_offline.services.remote_client import RemoteContext

PASSWORD = "StrongPass1"


def test_lan_user_administration_and_permission_denial():
    tmp = tempfile.mkdtemp(prefix="nano-lan-auth-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    server = LanApiServer(ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        admin = RemoteContext(base)
        info = admin.login("admin", PASSWORD)
        assert "admin_users" in info["permissions"]

        user_id = admin.auth.create_user(
            username="wh1", full_name="أمين المخزن", password=PASSWORD,
            role="warehouse",
            permissions=["dashboard", "items", "invoices_purchases", "suppliers", "reports", "security"],
        )
        users = {user["username"]: user for user in admin.auth.list_users()}
        assert users["wh1"]["role"] == "warehouse"
        assert "items" in users["wh1"]["permissions"]

        admin.auth.set_permissions(int(user_id), ["dashboard", "items"])
        admin.auth.update_user(int(user_id), full_name="أمين المخزن 2")
        users = {user["username"]: user for user in admin.auth.list_users()}
        assert users["wh1"]["permissions"] == ["dashboard", "items"]
        assert users["wh1"]["full_name"] == "أمين المخزن 2"

        admin.auth.reset_password(int(user_id), "TempPass99", force_change=True)
        limited = RemoteContext(base)
        limited_info = limited.login("wh1", "TempPass99")
        assert limited_info["must_change_password"] is True
        assert "admin_users" not in limited_info["permissions"]
        with pytest.raises(RuntimeError):
            limited.auth.list_users()
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)
