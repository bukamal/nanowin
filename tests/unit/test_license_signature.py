"""Signed-license contract: the app must trust only Worker-signed payloads.

The production public key lives in ``license_service``; here we swap in a
throwaway keypair so the tests can sign a payload the same way the Cloudflare
Worker does, without ever shipping the real private key in the repo.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from nano_offline.core.database import Database
from nano_offline.services import license_service
from nano_offline.services.license_service import LicenseService


def _sign(text: str, private_key) -> str:
    signature = private_key.sign(text.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


@pytest.fixture()
def signed_env(tmp_path, monkeypatch):
    db = Database(tmp_path / "license.db")
    db.initialize()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    monkeypatch.setattr(license_service, "LICENSE_PUBLIC_KEY_N", numbers.n)
    monkeypatch.setattr(license_service, "LICENSE_PUBLIC_KEY_E", numbers.e)
    service = LicenseService(db)
    return service, private_key, db


def _result(service: LicenseService, private_key, *, code="HAWA", edition="pro", expires_at=None, status="active"):
    payload = json.dumps(
        {
            "v": 2,
            "code": code,
            "fingerprint": service.device_id(),
            "edition": edition,
            "expiresAt": expires_at,
            "status": status,
            "issuedAt": 1,
        },
        ensure_ascii=False,
    )
    return {"success": True, "payload": payload, "signature": _sign(payload, private_key)}


def test_signed_result_activates_and_reports_edition(signed_env):
    service, private_key, _db = signed_env
    future = int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp() * 1000)
    status = service._install_hawaa_result("HAWA", _result(service, private_key, expires_at=future))
    assert status.valid is True
    assert status.edition == "pro"
    assert status.protocol == license_service.HAWAA_PROTOCOL_V2


def test_tampered_payload_is_rejected(signed_env):
    service, private_key, _db = signed_env
    result = _result(service, private_key, edition="pro")
    result["payload"] = result["payload"].replace("pro", "max")
    with pytest.raises(ValueError):
        service._install_hawaa_result("HAWA", result)


def test_unsigned_result_is_rejected(signed_env):
    service, _private_key, _db = signed_env
    with pytest.raises(ValueError):
        service._install_hawaa_result("HAWA", {"success": True, "edition": "pro"})


def test_forged_local_token_is_rejected(signed_env):
    service, _private_key, db = signed_env
    forged = f"{license_service.HAWAA_PROTOCOL_V2}:{base64.urlsafe_b64encode(b'{}').rstrip(b'=').decode()}.AAAA"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO license_state(id,license_key,signed_token,device_id,activated_at,last_verified_at,expires_at)"
            " VALUES(1,'HAWA',?,?,?,?,NULL)",
            (forged, service.device_id(), datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
    status = service.status()
    assert status.activated is True
    assert status.valid is False


def test_legacy_v1_token_requires_reactivation(signed_env):
    service, _private_key, db = signed_env
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO license_state(id,license_key,signed_token,device_id,activated_at,last_verified_at,expires_at)"
            " VALUES(1,'HAWA','hawaa-v1:abc.def',?,?,?,NULL)",
            (service.device_id(), datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
    status = service.status()
    assert status.valid is False
    assert "أعد التفعيل" in (status.reason or "")


def test_offline_grace_expires_after_window(signed_env):
    service, private_key, db = signed_env
    service._install_hawaa_result("HAWA", _result(service, private_key))
    stale = (datetime.now(timezone.utc) - timedelta(days=license_service.OFFLINE_GRACE_DAYS + 1)).isoformat()
    with db.transaction() as conn:
        conn.execute("UPDATE license_state SET last_verified_at=? WHERE id=1", (stale,))
    status = service.status()
    assert status.valid is False
    assert "مدة التحقق" in (status.reason or "")


def test_revalidate_keeps_license_when_offline(signed_env, monkeypatch):
    service, private_key, _db = signed_env
    service._install_hawaa_result("HAWA", _result(service, private_key))

    def _unreachable(*_a, **_k):
        raise license_service._ActivationError("تعذر الاتصال بخدمة التفعيل")

    monkeypatch.setattr(service, "_post_activation", _unreachable)
    assert service.revalidate().valid is True


def test_revalidate_drops_license_when_revoked(signed_env, monkeypatch):
    service, private_key, _db = signed_env
    service._install_hawaa_result("HAWA", _result(service, private_key))

    def _rejected(*_a, **_k):
        raise license_service._ActivationError("تم تعطيل المفتاح", rejected=True)

    monkeypatch.setattr(service, "_post_activation", _rejected)
    status = service.revalidate()
    assert status.valid is False
    assert service.status().activated is False
