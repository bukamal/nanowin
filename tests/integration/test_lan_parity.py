"""LAN parity: a remote client sees exactly what the local context computes.

Starts the real HTTP API server on loopback, logs in, performs one write and
then compares reads/documents against the local services. Complements
``tools/rpc_parity_smoke_test.py`` (which only checks that every method is
exposed) by checking the *results* match.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from nano_offline.app_context import AppContext
from nano_offline.services.invoice_service import InvoiceLineInput
from nano_offline.services.lan_api_server import LanApiServer
from nano_offline.services.remote_client import RemoteContext

PASSWORD = "StrongPass1"


def _new_ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-lan-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", PASSWORD)
    ctx.auth.login("admin", PASSWORD)
    return ctx, tmp


def test_remote_reads_and_writes_match_local():
    ctx, tmp = _new_ctx()
    server = LanApiServer(ctx, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        remote = RemoteContext(base)
        assert remote.login("admin", PASSWORD)["role"] == "admin"

        # --- write parity: created remotely, visible locally --------------- #
        remote_item_id = remote.items.create(name="منتج RPC", selling_price=3.0, purchase_price=1.0, quantity=10)
        local_item = ctx.items.get(int(remote_item_id))
        assert local_item is not None and local_item["name"] == "منتج RPC"

        local_invoice_id = ctx.invoices.create_invoice(
            invoice_type="sale", paid_amount=0,
            lines=[InvoiceLineInput(description="منتج RPC", item_id=int(remote_item_id), quantity=2, unit_price=3.0)],
        )

        # --- read parity: same catalogue names ---------------------------- #
        local_names = sorted(str(i["name"]) for i in ctx.items.list())
        remote_names = sorted(str(i["name"]) for i in remote.items.list())
        assert remote_names == local_names

        # --- document parity: identical rendered HTML --------------------- #
        local_html = ctx.documents.invoice_html(local_invoice_id)
        remote_html = remote.documents.invoice_html(invoice_id=local_invoice_id)
        assert remote_html == local_html

        # --- dashboard parity --------------------------------------------- #
        remote_summary = remote.dashboard.summary()
        local_summary = ctx.dashboard.summary()
        for key in ("sales", "purchases", "cogs", "expenses", "net_profit", "inventory_value", "cash"):
            assert float(remote_summary[key]) == pytest.approx(float(local_summary[key]), abs=1e-6)
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    assert server.running is False