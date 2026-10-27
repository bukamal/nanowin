from __future__ import annotations

"""Smoke test for the built-in read-only LAN price-check server.

Starts the server on an ephemeral loopback port (no fixed port so CI runs can
never clash) against a throwaway DB and checks the health/price/kiosk
surfaces, plus the admin settings plumbing.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from nano_offline.app_context import AppContext
from nano_offline.core import lan_settings
from nano_offline.core.fsutil import tempdir
from nano_offline.services.lan_price_server import LanPriceServer


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


with tempdir(prefix="qeid-lan-price-") as td:
    ctx = AppContext.create(Path(td) / "nano.db")
    unit = ctx.definitions.create_unit("قطعة", "ق")
    ctx.items.create(
        name="ماء صغير 330مل",
        barcode="6221031400012",
        selling_price=2.5,
        purchase_price=1.5,
        quantity=100,
        base_unit_id=unit,
    )
    kg = ctx.definitions.create_unit("كغ", "كغ")
    ctx.items.create(
        name="موز",
        barcode="2000000000017",
        selling_price=1.2,
        purchase_price=0.8,
        quantity=50,
        base_unit_id=kg,
    )

    server = LanPriceServer(
        ctx.db,
        ctx.settings,
        host="127.0.0.1",
        port=0,  # ephemeral
        company="متجر الاختبار",
        title="استعلام الأسعار",
        accent="#0B5FCC",
    )
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"

        status, body = _get(f"{base}/api/health")
        assert status == 200, status
        assert json.loads(body)["ok"] is True, body

        status, body = _get(f"{base}/api/price?barcode=6221031400012")
        found = json.loads(body)
        assert status == 200 and found["found"] is True, found
        assert found["name"] == "ماء صغير 330مل", found
        assert found["price_text"], found
        assert "category" in found, found

        status, body = _get(f"{base}/api/price?barcode=0000000000000")
        assert json.loads(body)["found"] is False, body

        # Weighted (weight/volume base unit) items are flagged for the kiosk.
        status, body = _get(f"{base}/api/price?barcode=2000000000017")
        banana = json.loads(body)
        assert banana["found"] is True and banana["weighted"] is True, banana

        # Name search (for kiosks without a scanner / a missing barcode).
        status, body = _get(f"{base}/api/search?q=" + urllib.parse.quote("ماء"))
        results = json.loads(body).get("results") or []
        assert status == 200 and results and results[0]["name"] == "ماء صغير 330مل", results

        status, body = _get(f"{base}/")
        page = body.decode("utf-8")
        assert status == 200, status
        assert 'dir="rtl"' in page and "/api/price" in page, page[:200]

        # Allow-list: a network that excludes the loopback client blocks it.
        ctx.settings.set_many({lan_settings.ALLOWED_SUBNETS_KEY: "10.0.0.0/8"})
        status, _ = _get(f"{base}/api/health")
        assert status == 403, status
        ctx.settings.set_many({lan_settings.ALLOWED_SUBNETS_KEY: "127.0.0.0/8"})
        status, _ = _get(f"{base}/api/health")
        assert status == 200, status
        ctx.settings.set_many({lan_settings.ALLOWED_SUBNETS_KEY: ""})

        status, _ = _get(f"{base}/api/secret-admin")
        assert status == 404, status
    finally:
        server.stop()
    assert server.running is False

    # Admin settings plumbing (defaults + parsing/validation).
    assert lan_settings.enabled({}) is False
    assert lan_settings.enabled({lan_settings.ENABLED_KEY: "1"}) is True
    assert lan_settings.port({}) == lan_settings.DEFAULT_PORT
    assert lan_settings.port({lan_settings.PORT_KEY: "9123"}) == 9123
    assert lan_settings.port({lan_settings.PORT_KEY: "7"}) == lan_settings.DEFAULT_PORT
    assert lan_settings.port({lan_settings.PORT_KEY: "abc"}) == lan_settings.DEFAULT_PORT

    assert lan_settings.idle_seconds({}) == lan_settings.DEFAULT_IDLE_SECONDS
    assert lan_settings.idle_seconds({lan_settings.IDLE_SECONDS_KEY: "20"}) == 20
    assert lan_settings.idle_seconds({lan_settings.IDLE_SECONDS_KEY: "1"}) == lan_settings.DEFAULT_IDLE_SECONDS

    assert lan_settings.client_allowed({}, "192.168.1.5") is True
    assert lan_settings.client_allowed({lan_settings.ALLOWED_SUBNETS_KEY: "192.168.0.0/16"}, "192.168.1.5") is True
    assert lan_settings.client_allowed({lan_settings.ALLOWED_SUBNETS_KEY: "192.168.0.0/16"}, "10.0.0.1") is False
    assert lan_settings.client_allowed({lan_settings.ALLOWED_SUBNETS_KEY: "10.0.0.0/8"}, "not-an-ip") is False

print("lan_price_server_smoke_test passed")
