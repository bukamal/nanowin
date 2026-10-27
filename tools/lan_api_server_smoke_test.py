from __future__ import annotations

"""Phase 1+2 smoke: LAN API server + RemoteTransport/RemoteContext client.

Phase 1: a read method returns real data over HTTP; writes are refused until
you log in; the bearer token and the shared subnet allow-list are enforced.
Phase 2: ``/api/login`` issues a per-user session, guarded writes run under it
(role-checked), and the audit trail is stamped with the acting user -- on the
server's writing thread, never another client's.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core import api_settings, lan_settings
from nano_offline.core.fsutil import tempdir
from nano_offline.core.transport import RemoteTransport
from nano_offline.services.invoice_service import InvoiceLineInput
from nano_offline.services.lan_api_server import LanApiServer
from nano_offline.services.remote_client import RemoteContext

PASSWORD = "password123"


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


with tempdir(prefix="qeid-lan-api-") as td:
    ctx = AppContext.create(Path(td) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.auth.login("admin", PASSWORD)  # establish a local admin session to add users
    ctx.auth.create_user(username="viewer", full_name="مشاهد", password=PASSWORD, role="viewer")

    ctx.items.create(
        name="ماء صغير", barcode="6221031400012", selling_price=2.5, purchase_price=1.5, quantity=10
    )
    invoice_id = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="ماء صغير", quantity=1, unit_price=2.5)],
        paid_amount=0,
    )

    server = LanApiServer(ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"

        status, body = _get(f"{base}/api/health")
        assert status == 200 and json.loads(body)["ok"] is True, body

        remote = RemoteContext(base)

        # --- Phase 1: reads over the LAN -------------------------------
        rows = remote.items.list(search="ماء")
        assert rows and rows[0]["name"] == "ماء صغير", rows
        assert remote.items.find_by_barcode(barcode="6221031400012")["name"] == "ماء صغير"
        assert "ماء" in remote.documents.invoice_html(invoice_id=invoice_id)

        # A write without a session is refused (401).
        try:
            RemoteTransport(base).call("items", "create", name="x", selling_price=1, purchase_price=1)
            raise AssertionError("unsigned write should be refused")
        except RuntimeError as exc:
            assert "401" in str(exc), exc

        # A non-listed method is refused (403).
        try:
            remote.settings.set(key="x", value="1")
            raise AssertionError("settings.set should be refused")
        except RuntimeError as exc:
            assert "403" in str(exc) or "مسموح" in str(exc), exc

        # --- Phase 2: login, guarded writes, audit attribution ----------
        user = remote.login("admin", PASSWORD)
        assert user["role"] == "admin", user

        new_item = remote.items.create(name="منتج RPC", selling_price=3.0, purchase_price=1.0)
        assert isinstance(new_item, int), new_item

        created = remote.invoices.create_invoice(
            invoice_type="sale",
            lines=[{"description": "منتج RPC", "quantity": 2, "unit_price": 3.0}],
            paid_amount=0,
        )
        assert isinstance(created, int), created

        with ctx.db.connect() as conn:
            row = conn.execute(
                "SELECT username FROM audit_log WHERE action='create' AND entity_type='item' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None and row["username"] == "admin", row

        # A non-privileged role cannot write.
        viewer = RemoteContext(base)
        viewer.login("viewer", PASSWORD)
        try:
            viewer.items.create(name="ممنوع", selling_price=1.0, purchase_price=1.0)
            raise AssertionError("viewer write should be refused")
        except RuntimeError as exc:
            assert "403" in str(exc) or "صلاحية" in str(exc), exc

        # After logout the session is dead.
        remote.logout()
        try:
            remote.items.create(name="بعد الخروج", selling_price=1.0, purchase_price=1.0)
            raise AssertionError("write after logout should be refused")
        except RuntimeError as exc:
            assert "401" in str(exc), exc
    finally:
        server.stop()
    assert server.running is False

    # Bearer token enforcement (transport-level shared secret).
    guarded = LanApiServer(ctx, host="127.0.0.1", port=0, token="secret123")
    guarded.start()
    try:
        tbase = f"http://127.0.0.1:{guarded.port}"
        try:
            RemoteTransport(tbase).call("items", "list")
            raise AssertionError("missing token should be rejected")
        except RuntimeError as exc:
            assert "401" in str(exc), exc
        assert isinstance(RemoteTransport(tbase, token="secret123").call("items", "list"), list)
    finally:
        guarded.stop()

    # Shared subnet allow-list blocks a disallowed client.
    ctx.settings.set_many({lan_settings.ALLOWED_SUBNETS_KEY: "10.0.0.0/8"})
    limited = LanApiServer(ctx, host="127.0.0.1", port=0)
    limited.start()
    try:
        status, _ = _get(f"http://127.0.0.1:{limited.port}/api/health")
        assert status == 403, status
    finally:
        limited.stop()
        ctx.settings.set_many({lan_settings.ALLOWED_SUBNETS_KEY: ""})

    # Settings plumbing.
    assert api_settings.enabled({}) is False
    assert api_settings.port({api_settings.PORT_KEY: "9123"}) == 9123
    assert api_settings.token({api_settings.TOKEN_KEY: " abc "}) == "abc"

print("lan_api_server_smoke_test passed")
