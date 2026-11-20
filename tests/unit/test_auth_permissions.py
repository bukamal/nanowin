from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.services.auth_service import (
    CAPABILITIES,
    ROLE_PERMISSIONS,
    UserSession,
    effective_permissions,
    overrides_json,
)

PASSWORD = "StrongPass1"


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-auth-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.auth.login("admin", PASSWORD)
    return ctx, tmp


def test_role_presets_cover_the_five_roles():
    assert set(ROLE_PERMISSIONS) == {"admin", "accountant", "sales", "warehouse", "viewer"}
    cashier = ROLE_PERMISSIONS["sales"]
    assert "invoices_sales" in cashier and "invoices_purchases" not in cashier
    assert "admin_users" not in cashier
    warehouse = ROLE_PERMISSIONS["warehouse"]
    assert "items" in warehouse and "invoices_purchases" in warehouse
    assert "invoices_sales" not in warehouse


def test_derived_module_keys():
    cashier = UserSession(1, "c", "C", "sales", ROLE_PERMISSIONS["sales"])
    assert cashier.can("invoices") is True
    assert cashier.can("sales") is True and cashier.can("purchases") is False
    assert cashier.can("admin") is False
    warehouse = UserSession(2, "w", "W", "warehouse", ROLE_PERMISSIONS["warehouse"])
    assert warehouse.can("invoices") is True
    assert warehouse.can("invoices_purchases") is True
    assert warehouse.can("invoices_sales") is False
    assert warehouse.can("purchases") is True and warehouse.can("sales") is False


def test_overrides_round_trip():
    role = "warehouse"
    granted = set(ROLE_PERMISSIONS[role]) | {"view_cost"} - {"suppliers"}
    payload = overrides_json(role, granted)
    assert payload
    assert effective_permissions(role, payload) == frozenset(granted)
    assert overrides_json(role, set(ROLE_PERMISSIONS[role])) is None


def test_create_user_permissions_and_password_flows():
    ctx, tmp = _ctx()
    try:
        user_id = ctx.auth.create_user(
            username="cashier", full_name="كاشير", password=PASSWORD,
            role="sales", must_change_password=True,
        )
        users = {user["username"]: user for user in ctx.auth.list_users()}
        assert users["cashier"]["role"] == "sales"
        assert "invoices_sales" in users["cashier"]["permissions"]
        session = ctx.auth.verify_credentials("cashier", PASSWORD)
        assert session is not None and session.must_change_password is True

        ctx.auth.reset_password(user_id, "AnotherPass2", force_change=True)
        session = ctx.auth.verify_credentials("cashier", "AnotherPass2")
        assert session is not None and session.must_change_password is True

        ctx.auth.logout()
        ctx.auth.login("cashier", "AnotherPass2")
        ctx.auth.change_own_password("AnotherPass2", "ThirdPass3")
        assert ctx.auth.current() is not None
        assert ctx.auth.current().must_change_password is False
        assert ctx.auth.verify_credentials("cashier", "ThirdPass3") is not None
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_last_admin_cannot_lose_user_administration():
    ctx, tmp = _ctx()
    try:
        admin = ctx.auth.list_users()[0]
        with pytest.raises(ValueError):
            ctx.auth.set_permissions(admin["id"], {"dashboard", "items"})
        # Adding a second active admin makes the change legal.
        ctx.auth.create_user(
            username="admin2", full_name="مدير 2", password=PASSWORD, role="admin"
        )
        ctx.auth.set_permissions(admin["id"], {"dashboard", "items", "admin_users"})
        assert ctx.auth.verify_credentials("admin", PASSWORD).can("admin_users")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_capability_catalogue_shape():
    for key, (label, group) in CAPABILITIES.items():
        assert key and label and group in {"access", "sensitive"}


def test_delete_user_guards_and_audit_retention():
    ctx, tmp = _ctx()
    try:
        admin = ctx.auth.current()
        assert admin is not None
        uid = ctx.auth.create_user(
            username="temp1", full_name="مؤقت", password=PASSWORD, role="sales"
        )
        with pytest.raises(ValueError):
            ctx.auth.delete_user(admin.id)
        ctx.auth.delete_user(uid)
        assert all(user["id"] != uid for user in ctx.auth.list_users())
        # The audit trail keeps the username even after the account is gone.
        with ctx.db.connect() as conn:
            rows = conn.execute(
                "SELECT details FROM audit_log WHERE action='delete' AND entity_type='user'"
            ).fetchall()
        assert rows and "temp1" in str(rows[-1][0])
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
