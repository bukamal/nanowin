from __future__ import annotations

"""Phase 7 smoke: RPC value parity.

Services return dataclasses (Alert/Decision/OwnerPulse); the server serialises
them to dicts and the client wraps them back into attribute-accessible objects,
so client code keeps using ``pulse.headline`` / ``alert.title`` unchanged.
"""

from dataclasses import dataclass
from pathlib import Path

import asyncio

from nano_offline.app_context import AppContext
from nano_offline.core.fsutil import tempdir
from nano_offline.core.transport import AttrDict, _wrap
from nano_offline.services.lan_api_server import LanApiServer, _jsonable
from nano_offline.services.remote_client import RemoteContext

PASSWORD = "password123"


@dataclass
class _Thing:
    title: str
    metrics: dict
    child: "_Thing | None" = None


# --- unit: server-side conversion + client-side wrapping -------------------
thing = _Thing(title="x", metrics={"a": 1}, child=_Thing("y", {"b": 2}))
as_dict = _jsonable(thing)
assert as_dict == {"title": "x", "metrics": {"a": 1}, "child": {"title": "y", "metrics": {"b": 2}, "child": None}}, as_dict
wrapped = _wrap(as_dict)
assert isinstance(wrapped, AttrDict) and wrapped.title == "x"
assert wrapped["title"] == "x"
assert wrapped.metrics["a"] == 1 and wrapped.child.title == "y"

with tempdir(prefix="qeid-parity-") as td:
    ctx = AppContext.create(Path(td) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.items.create(name="خبز", selling_price=2.0, purchase_price=1.0, quantity=10)

    server = LanApiServer(ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        remote = RemoteContext(base)
        remote.login("admin", PASSWORD)

        # A dataclass-returning read round-trips with attribute access.
        pulse = remote.owner_pulse.generate()
        assert isinstance(pulse, AttrDict), type(pulse)
        assert isinstance(pulse.headline, str) and pulse.headline
        assert isinstance(pulse.severity, str)
        assert pulse["headline"] == pulse.headline

        # List-returning reads come back as lists of attribute-accessible items.
        alerts = remote.notifications.generate_alerts()
        decisions = remote.smart_assistant.decisions()
        assert isinstance(alerts, list) and isinstance(decisions, list)
        for alert in alerts:
            assert hasattr(alert, "severity") and hasattr(alert, "title")

        # Phase 8: read parity for the remaining business services.
        assert isinstance(remote.expenses.list_expenses(), list)
        assert isinstance(remote.expenses.list_categories(), list)
        assert isinstance(remote.notifications.get_config(), dict)
        assert isinstance(remote.notifications.unread_summary(), dict)
        assert remote.cash_day_close.today_cash_movement() is not None
        assert isinstance(remote.external_notifications.get_config(), dict)

        # Phase 9: async ``.aio`` bridge off-loads the HTTP call.
        rows = asyncio.run(remote.items.aio.list(search="خبز"))
        assert rows and rows[0]["name"] == "خبز"

        # Phase 9: user administration over RPC (admin actor).
        assert isinstance(remote.auth.list_users(), list)
        new_uid = remote.auth.create_user(
            username="cashier2", full_name="كاشير", password="password123", role="sales"
        )
        assert isinstance(new_uid, int)
        assert any(u.get("username") == "cashier2" for u in remote.auth.list_users())

        # A non-admin cannot administer users.
        remote.auth.create_user(username="viewer", full_name="مشاهد", password="password123", role="viewer")
        viewer = RemoteContext(base)
        viewer.login("viewer", "password123")
        try:
            viewer.auth.create_user(username="x", full_name="x", password="password123", role="sales")
            raise AssertionError("viewer must not create users")
        except RuntimeError as exc:
            assert "403" in str(exc) or "صلاحية" in str(exc), exc
    finally:
        server.stop()

print("rpc_parity_smoke_test passed")
