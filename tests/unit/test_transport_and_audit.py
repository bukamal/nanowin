from __future__ import annotations

"""Phase 0 of the client/server work: the service-level transport seam and the
audit service that replaced raw SQL in the views.

Every data operation must be reachable generically as
``transport.call(service, method, **kwargs)`` -- that is the single contract the
future LAN server (dispatch) and remote client (HTTP) share.
"""

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core.transport import LocalTransport, Transport
from nano_offline.services.invoice_service import InvoiceLineInput


def _ctx(tmp_path) -> AppContext:
    return AppContext.create(tmp_path / "t.db")


def test_local_transport_dispatches_to_services(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.items.create(name="ماء", selling_price=2.0, purchase_price=1.0, quantity=10)

    transport = LocalTransport(ctx)
    assert isinstance(transport, Transport)

    rows = transport.call("items", "list", search="ماء")
    assert rows and rows[0]["name"] == "ماء"
    assert transport.call("items", "find_by_barcode", barcode="000000") is None


def test_local_transport_rejects_unknown_service_or_method(tmp_path):
    transport = LocalTransport(_ctx(tmp_path))
    with pytest.raises(AttributeError):
        transport.call("nope", "list")
    with pytest.raises(AttributeError):
        transport.call("items", "nope")


def test_audit_service_logs_and_counts_prints(tmp_path):
    ctx = _ctx(tmp_path)
    invoice_id = ctx.invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="مادة", quantity=1, unit_price=10)],
        paid_amount=0,
    )

    assert ctx.audit.log_print(entity_type="invoice", entity_id=invoice_id, channel="print") == 1
    assert ctx.audit.log_print(entity_type="invoice", entity_id=invoice_id, channel="pos") == 2
