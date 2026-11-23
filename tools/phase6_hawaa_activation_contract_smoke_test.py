from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from nano_offline.app_context import AppContext
from nano_offline.services import license_service
from nano_offline.services.license_service import (
    HAWAA_ACTIVATION_URL,
    HAWAA_PROTOCOL_V2,
    LicenseService,
)
from nano_offline.core.fsutil import tempdir


class FakeResponse:
    status = 200

    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


def _sign(text: str, private_key) -> str:
    signature = private_key.sign(text.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


with tempdir(prefix="nano_phase6_hawaa_license_") as td:
    ctx = AppContext.create(Path(td) / "nano.db")

    # The production Worker signs with its private SIGNING_KEY; the app verifies
    # with an embedded public key. Swap in a throwaway keypair so the contract
    # can be checked without shipping the real private key.
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    license_service.LICENSE_PUBLIC_KEY_N = numbers.n
    license_service.LICENSE_PUBLIC_KEY_E = numbers.e

    expiration_ms = 4102444800000  # 2100-01-01
    payload_text = json.dumps(
        {
            "v": 2,
            "code": "QED-TEST-HAWAA",
            "fingerprint": ctx.license.device_id(),
            "edition": "standard",
            "expiresAt": expiration_ms,
            "status": "active",
            "issuedAt": 1,
        }
    )
    signed_body = {
        "success": True,
        "payload": payload_text,
        "signature": _sign(payload_text, private_key),
        "edition": "standard",
        "expiresAt": expiration_ms,
        "expirationDate": expiration_ms,
    }

    captured = {}
    original = urllib.request.urlopen

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["user_agent"] = request.get_header("User-agent")
        return FakeResponse(signed_body)

    urllib.request.urlopen = fake_urlopen
    try:
        status = ctx.license.activate_online("QED-TEST-HAWAA", "0.6.0")
    finally:
        urllib.request.urlopen = original

    assert captured["url"] == HAWAA_ACTIVATION_URL
    assert set(captured["body"]) == {"licenseCode", "fingerprint"}
    assert captured["body"]["licenseCode"] == "QED-TEST-HAWAA"
    assert captured["body"]["fingerprint"] == ctx.license.device_id()
    assert captured["user_agent"] == "Nano-Offline/0.6.0"
    serialized = json.dumps(captured["body"], ensure_ascii=False).lower()
    for forbidden in ["customer", "supplier", "invoice", "inventory", "report", "user"]:
        assert forbidden not in serialized

    assert status.valid and status.protocol == HAWAA_PROTOCOL_V2
    assert status.license_key == "QED-TEST-HAWAA"
    assert status.edition == "standard"

    # Offline restart: status must not perform any HTTP request.
    def network_forbidden(*args, **kwargs):
        raise AssertionError("offline status unexpectedly accessed the network")

    urllib.request.urlopen = network_forbidden
    try:
        offline = LicenseService(ctx.db).status(now=date(2029, 1, 1))
    finally:
        urllib.request.urlopen = original
    assert offline.valid and offline.protocol == HAWAA_PROTOCOL_V2
    assert not LicenseService(ctx.db).status(now=date(2101, 1, 1)).valid

    # Cloudflare Error 1010 must be translated to a concise, actionable message
    # instead of leaking the raw JSON/HTML response into the activation UI.
    cloudflare_1010 = json.dumps({
        "status": 403,
        "error_code": 1010,
        "error_name": "browser_signature_banned",
        "detail": "The site owner has blocked access based on your browser's signature.",
    }).encode("utf-8")

    def blocked_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(cloudflare_1010),
        )

    urllib.request.urlopen = blocked_urlopen
    try:
        try:
            ctx.license.activate_online("QED-BLOCKED", "0.7.0")
        except RuntimeError as exc:
            error_text = str(exc)
        else:
            raise AssertionError("Cloudflare 1010 unexpectedly succeeded")
    finally:
        urllib.request.urlopen = original
    assert "Error 1010" in error_text
    assert "إعادة المحاولة الآن لن تفيد" in error_text
    assert "browser_signature_banned" not in error_text

    # Local tampering breaks the signed token, so the license must be rejected.
    with ctx.db.transaction() as conn:
        token = str(conn.execute("SELECT signed_token FROM license_state WHERE id=1").fetchone()[0])
        conn.execute("UPDATE license_state SET signed_token=? WHERE id=1", (token[:-1] + ("A" if token[-1] != "A" else "B"),))
    assert not LicenseService(ctx.db).status(now=date(2029, 1, 1)).valid

print("phase6_hawaa_activation_contract_smoke_test passed")
