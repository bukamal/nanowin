from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import platform
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone

from nano_offline.core.database import Database

# Same production endpoint/protocol used by Hawaa Al-Sham.
HAWAA_ACTIVATION_URL = "https://license.manhal-almasriiii199119.workers.dev/activate"
HAWAA_REVALIDATE_URL = HAWAA_ACTIVATION_URL.rsplit("/", 1)[0] + "/revalidate"
HAWAA_PROTOCOL = "hawaa-v1"  # legacy local-seal tokens (pre signature upgrade)
HAWAA_PROTOCOL_V2 = "hawaa-v2"  # server-signed payload, verified offline
TOKEN_ALGORITHM = "RS256"  # phase-5 compatibility for already issued signed tokens

# Public key of the activation Worker (matches the SIGNING_KEY secret). It is
# embedded in code on purpose: a key read from the (local, editable) settings
# table could be swapped by an attacker to validate a forged payload.
LICENSE_PUBLIC_KEY_N = int(
    "c1c95aae0514d70c4dc109159a78551dac1eb8d435e1a871577ab2799a631c5b"
    "628d605965ae0256a56391e935c32a28642c454a7aaa474213a2700efd9c6289"
    "d8f6c4763f1d1b1e3f71167402c213a2c5fc4977c5947753718efaba9b85c4dc"
    "9b91051bc67371f89f2541c9fab8a0240d6f42821f76d4db281a7812bf15b658"
    "ae816874f645e32405429989080f648f85fcee6c30e104e8aab35a7b22dc786d"
    "d57a6b3ac7bc8cc5773c9f9ce7cb681be0b18047ed5c4ff5301338db3afaf9db"
    "838a7acbd7294c9450e7f83e879cc59b2782eae5fabfda88282563de3af173bd"
    "261858ae82cd00b1e72fd7148525a3c9a4b22afd6e79e01254dbae4a2edb894b",
    16,
)
LICENSE_PUBLIC_KEY_E = 65537

# The production Worker answers errors as plain English text. Map the known
# ones to clear Arabic so the UI never shows a raw server string. Unknown
# messages still fall back to the server text.
_WORKER_ERROR_MESSAGES = {
    "invalid license code": "مفتاح الترخيص غير صالح أو غير مدرج لدى الخادم",
    "device fingerprint required": "معرّف الجهاز مطلوب لإتمام التفعيل",
    "license revoked": "هذا الترخيص موقوف أو معطّل من الإدارة",
    "license expired": "انتهت صلاحية هذا الترخيص",
    "maximum device limit reached": "بلغ المفتاح الحد الأقصى لعدد الأجهزة المسموح بها",
    "device not registered": "الجهاز غير مسجّل لدى الخادم — أعد التفعيل بالمفتاح",
}

# Offline-first POS must keep selling without internet, but an admin who
# disables/extends/expires a key in the Telegram bot must win. So a device is
# re-checked online periodically; if it cannot reach the server it stays valid
# only for this grace window, after which it must reconnect once.
OFFLINE_GRACE_DAYS = 14
REVALIDATE_INTERVAL_HOURS = 6


class _ActivationError(Exception):
    """Raised by ``_post_activation``.

    ``rejected`` distinguishes a definitive server answer (bad/revoked/expired
    key -> the local license must be dropped) from a transient problem
    (offline, server error, Cloudflare block -> keep the grace window).
    """

    def __init__(self, message: str, *, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


@dataclass(slots=True, frozen=True)
class LicenseStatus:
    activated: bool
    valid: bool = False
    license_key: str | None = None
    edition: str | None = None
    expires_at: str | None = None
    device_id: str | None = None
    reason: str | None = None
    protocol: str | None = None


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def verify_rs256(message: bytes, signature: bytes, modulus: int, exponent: int = 65537) -> bool:
    """Verify an RSASSA-PKCS1-v1_5 SHA-256 signature using only stdlib.

    Kept for phase-5 signed-token compatibility. The production activation path
    in phase 6 uses the same Hawaa Al-Sham Worker contract instead.
    """
    if modulus <= 0 or exponent <= 1:
        return False
    k = (modulus.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= modulus:
        return False
    em = pow(sig_int, exponent, modulus).to_bytes(k, "big")
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    digest = hashlib.sha256(message).digest()
    tail = digest_info_prefix + digest
    if k < len(tail) + 11:
        return False
    expected = b"\x00\x01" + (b"\xff" * (k - len(tail) - 3)) + b"\x00" + tail
    return secrets.compare_digest(em, expected)


def _parse_expiration(value):
    """Parse the expiration variants accepted by the Hawaa activation client."""
    if value in (None, ""):
        return "unknown", None
    text = str(value).strip()
    lifetime = {
        "lifetime",
        "unlimited",
        "permanent",
        "never",
        "غير محدود",
        "مدى الحياة",
        "لا ينتهي",
    }
    if text.lower() in lifetime or text in lifetime:
        return "lifetime", None
    try:
        num = float(text)
        if num > 10_000_000_000:  # milliseconds
            num /= 1000
        return "date", datetime.fromtimestamp(num, timezone.utc).replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return "date", datetime.strptime(text, fmt)
        except Exception:
            pass
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return "date", dt
    except Exception:
        return "invalid", None


class LicenseService:
    """Hawaa-compatible online activation with offline local checking.

    Network traffic is activation-only and matches the existing Hawaa Al-Sham
    Worker contract exactly: ``licenseCode`` and ``fingerprint``. Accounting,
    inventory, parties, users and reports are never sent to the activation
    server.

    The Hawaa Worker response is not cryptographically signed. Therefore the
    local seal below protects integrity/binding against ordinary file editing,
    but cannot provide the same server-authenticity guarantee as a signed token.
    Changing that property requires a compatible server-side protocol upgrade.
    """

    def __init__(self, db: Database):
        self.db = db

    def device_id(self) -> str:
        """Use the same fingerprint algorithm as the Hawaa Al-Sham client."""
        try:
            username = getpass.getuser()
        except Exception:
            username = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        info = (
            platform.node()
            + platform.processor()
            + username
            + platform.system()
            + platform.machine()
        )
        return hashlib.sha256(info.encode()).hexdigest()

    @staticmethod
    def server_url() -> str:
        # Fixed endpoint. The signed payload is verified against the embedded
        # public key, so pointing the client elsewhere cannot produce a valid
        # license anyway; removing the override closes that door completely.
        return HAWAA_ACTIVATION_URL

    def activation_url(self) -> str:
        return self.server_url()

    def set_activation_url(self, url: str) -> None:
        """Compatibility shim: production UI no longer makes the URL editable."""
        value = (url or "").strip().rstrip("/")
        expected = HAWAA_ACTIVATION_URL.rstrip("/")
        if value and value != expected:
            raise ValueError("إعدادات خدمة التفعيل ثابتة ولا يمكن تعديلها من التطبيق")

    # ---- Phase-5 signed-token compatibility ---------------------------------
    def _configured_public_key(self) -> tuple[int, int] | None:
        with self.db.connect() as conn:
            rows = {
                str(r["key"]): str(r["value"])
                for r in conn.execute(
                    "SELECT key,value FROM settings WHERE key IN ('license_public_key_n','license_public_key_e')"
                ).fetchall()
            }
        n_text = rows.get("license_public_key_n", "").strip()
        if not n_text:
            return None
        try:
            modulus = int(n_text, 0)
            exponent = int(rows.get("license_public_key_e", "65537"), 0)
        except ValueError:
            return None
        return modulus, exponent

    def configure_public_key(self, modulus: int, exponent: int = 65537) -> None:
        if modulus <= 0 or exponent <= 1:
            raise ValueError("المفتاح العام غير صالح")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('license_public_key_n',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(modulus),),
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('license_public_key_e',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(exponent),),
            )

    def decode_and_verify(self, signed_token: str, *, modulus: int | None = None, exponent: int = 65537) -> dict:
        try:
            payload_part, signature_part = signed_token.split(".", 1)
            payload_bytes = _b64url_decode(payload_part)
            signature = _b64url_decode(signature_part)
            # Require canonical Base64URL spellings. Otherwise a mutation of
            # unused trailing Base64 bits can decode to the exact same signature.
            if _b64url_encode(payload_bytes) != payload_part or _b64url_encode(signature) != signature_part:
                raise ValueError("رمز التفعيل غير صالح")
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("رمز التفعيل غير صالح") from exc
        if modulus is None:
            configured = self._configured_public_key()
            if not configured:
                raise RuntimeError("المفتاح العام لخادم التفعيل غير مضبوط")
            modulus, exponent = configured
        if not verify_rs256(payload_part.encode("ascii"), signature, int(modulus), int(exponent)):
            raise ValueError("توقيع رمز التفعيل غير صحيح")
        if payload.get("alg") not in (None, TOKEN_ALGORITHM):
            raise ValueError("خوارزمية التوقيع غير مدعومة")
        return payload

    def install_signed_token(
        self,
        license_key: str,
        signed_token: str,
        *,
        modulus: int | None = None,
        exponent: int = 65537,
        now: date | None = None,
    ) -> LicenseStatus:
        payload = self.decode_and_verify(signed_token, modulus=modulus, exponent=exponent)
        device_id = self.device_id()
        if str(payload.get("device_id", "")) != device_id:
            raise ValueError("رمز التفعيل صادر لجهاز آخر")
        token_key = str(payload.get("license_key", "") or "")
        if token_key and token_key != (license_key or "").strip():
            raise ValueError("مفتاح الترخيص لا يطابق رمز التفعيل")
        expires_at = str(payload.get("expires_at") or "") or None
        if expires_at:
            try:
                expiry = date.fromisoformat(expires_at[:10])
            except ValueError as exc:
                raise ValueError("تاريخ انتهاء الترخيص غير صالح") from exc
            if expiry < (now or date.today()):
                raise ValueError("رمز التفعيل منتهي الصلاحية")
        activated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO license_state(id,license_key,signed_token,device_id,activated_at,last_verified_at,expires_at)
                   VALUES(1,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     license_key=excluded.license_key,signed_token=excluded.signed_token,device_id=excluded.device_id,
                     activated_at=excluded.activated_at,last_verified_at=excluded.last_verified_at,expires_at=excluded.expires_at""",
                ((license_key or "").strip(), signed_token, device_id, activated_at, activated_at, expires_at),
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('license_edition',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(payload.get("edition") or "standard"),),
            )
        return self.status(now=now)

    # ---- Hawaa production protocol ------------------------------------------
    def _verify_signed_payload(self, payload_text: str, signature: str) -> dict:
        """Verify a Worker-signed JSON payload with the embedded public key.

        The signature is the only thing that proves a payload really came from
        the activation server, so it is mandatory: without it any local file edit
        could fake a license.
        """
        try:
            signature_bytes = _b64url_decode(signature)
            if _b64url_encode(signature_bytes) != signature:
                raise ValueError("non-canonical signature")
        except Exception as exc:
            raise ValueError("توقيع الترخيص غير صالح") from exc
        if not verify_rs256(
            payload_text.encode("utf-8"),
            signature_bytes,
            LICENSE_PUBLIC_KEY_N,
            LICENSE_PUBLIC_KEY_E,
        ):
            raise ValueError("توقيع الترخيص غير مطابق لخادم التفعيل")
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError("بيانات الترخيص الموقّعة غير صالحة") from exc
        if not isinstance(payload, dict):
            raise ValueError("بيانات الترخيص الموقّعة غير صالحة")
        return payload

    def _seal_signed_payload(self, payload_text: str, signature: str) -> str:
        return f"{HAWAA_PROTOCOL_V2}:{_b64url_encode(payload_text.encode('utf-8'))}.{signature}"

    def _unseal_signed_payload(self, token: str) -> dict:
        if not token.startswith(f"{HAWAA_PROTOCOL_V2}:"):
            raise ValueError("صيغة بيانات الترخيص غير صحيحة")
        body = token.split(":", 1)[1]
        payload_b64, signature = body.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        if _b64url_encode(payload_bytes) != payload_b64:
            raise ValueError("بيانات الترخيص المحلية معدلة")
        return self._verify_signed_payload(payload_bytes.decode("utf-8"), signature)

    def _install_hawaa_result(self, license_key: str, result: dict, *, now: datetime | None = None) -> LicenseStatus:
        payload_text = result.get("payload")
        signature = result.get("signature")
        if not isinstance(payload_text, str) or not isinstance(signature, str):
            raise ValueError("خادم التفعيل لم يُرجع توقيعًا صالحًا")
        signed = self._verify_signed_payload(payload_text, signature)
        device_id = self.device_id()
        if str(signed.get("fingerprint") or "") != device_id:
            raise ValueError("رمز التفعيل صادر لجهاز آخر")
        key = (license_key or "").strip()
        if str(signed.get("code") or "") != key:
            raise ValueError("مفتاح الترخيص لا يطابق الرمز الموقّع")
        if str(signed.get("status") or "active") != "active":
            raise ValueError("الترخيص غير نشط لدى الخادم")
        kind, parsed = _parse_expiration(signed.get("expiresAt"))
        if kind == "invalid":
            raise ValueError("تاريخ انتهاء الترخيص الذي أعاده الخادم غير مفهوم")
        current = now or datetime.now(timezone.utc).replace(tzinfo=None)
        if kind == "date" and parsed and current > parsed:
            raise ValueError("الترخيص منتهي الصلاحية")
        activated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        expires_at = parsed.isoformat(timespec="seconds") if parsed else None
        edition = str(signed.get("edition") or "standard")
        signed_token = self._seal_signed_payload(payload_text, signature)
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO license_state(id,license_key,signed_token,device_id,activated_at,last_verified_at,expires_at)
                   VALUES(1,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     license_key=excluded.license_key,signed_token=excluded.signed_token,device_id=excluded.device_id,
                     activated_at=excluded.activated_at,last_verified_at=excluded.last_verified_at,expires_at=excluded.expires_at""",
                (key, signed_token, device_id, activated_at, activated_at, expires_at),
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('license_edition',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (edition,),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('activate','license',1,?)",
                (f"protocol={HAWAA_PROTOCOL_V2}; server=hawaa; edition={edition}",),
            )
        return self.status(now=current.date())

    def status(self, *, now: date | None = None) -> LicenseStatus:
        device_id = self.device_id()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM license_state WHERE id=1").fetchone()
            edition_row = conn.execute("SELECT value FROM settings WHERE key='license_edition'").fetchone()
        if not row or not row["signed_token"]:
            return LicenseStatus(False, False, device_id=device_id, reason="غير مفعل")
        if str(row["device_id"] or "") != device_id:
            return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, "الجهاز لا يطابق الترخيص")

        token = str(row["signed_token"])
        if token.startswith(f"{HAWAA_PROTOCOL_V2}:"):
            try:
                payload = self._unseal_signed_payload(token)
            except Exception as exc:
                return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, str(exc), HAWAA_PROTOCOL_V2)
            if str(payload.get("fingerprint") or "") != device_id:
                return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, "الترخيص يخص جهازًا آخر", HAWAA_PROTOCOL_V2)
            if str(payload.get("code") or "") != str(row["license_key"] or ""):
                return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, "بيانات مفتاح الترخيص معدلة", HAWAA_PROTOCOL_V2)
            if str(payload.get("status") or "active") != "active":
                return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, "الترخيص غير نشط لدى الخادم", HAWAA_PROTOCOL_V2)
            kind, parsed = _parse_expiration(payload.get("expiresAt"))
            if kind == "invalid":
                return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, "تاريخ انتهاء غير صالح", HAWAA_PROTOCOL_V2)
            today = now or date.today()
            if kind == "date" and parsed and parsed.date() < today:
                return LicenseStatus(True, False, row["license_key"], str(payload.get("edition") or "standard"), parsed.isoformat(timespec="seconds"), device_id, "الترخيص منتهي", HAWAA_PROTOCOL_V2)
            # Offline grace: a device that has not reached the server for longer
            # than OFFLINE_GRACE_DAYS must reconnect once, so a key disabled in
            # the Telegram bot cannot stay alive forever on an offline device.
            last_raw = str(row["last_verified_at"] or "")
            if last_raw:
                try:
                    if (today - datetime.fromisoformat(last_raw).date()).days > OFFLINE_GRACE_DAYS:
                        return LicenseStatus(
                            True, False, row["license_key"],
                            str(payload.get("edition") or "standard"),
                            parsed.isoformat(timespec="seconds") if parsed else None,
                            device_id,
                            f"انقضت مدة التحقق ({OFFLINE_GRACE_DAYS} يومًا) — يلزم الاتصال بالإنترنت مرة واحدة",
                            HAWAA_PROTOCOL_V2,
                        )
                except ValueError:
                    pass
            edition = str(payload.get("edition") or (edition_row[0] if edition_row else "standard"))
            expires = parsed.isoformat(timespec="seconds") if parsed else None
            return LicenseStatus(True, True, row["license_key"], edition, expires, device_id, None, HAWAA_PROTOCOL_V2)

        if token.startswith(f"{HAWAA_PROTOCOL}:"):
            # Legacy unsigned local seal: no longer trusted, so a device that
            # still holds one must re-activate online once.
            return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, "الترخيص بصيغة قديمة — أعد التفعيل عبر الإنترنت", HAWAA_PROTOCOL)

        # Legacy phase-5 RS256 local token.
        try:
            payload = self.decode_and_verify(token)
        except Exception as exc:
            return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, str(exc), "rs256-v1")
        if str(payload.get("device_id", "")) != device_id:
            return LicenseStatus(True, False, row["license_key"], None, row["expires_at"], device_id, "رمز التفعيل لجهاز آخر", "rs256-v1")
        expires_at = str(row["expires_at"] or "") or None
        if expires_at:
            try:
                if date.fromisoformat(expires_at[:10]) < (now or date.today()):
                    return LicenseStatus(True, False, row["license_key"], str(payload.get("edition") or "standard"), expires_at, device_id, "الترخيص منتهي", "rs256-v1")
            except ValueError:
                return LicenseStatus(True, False, row["license_key"], None, expires_at, device_id, "تاريخ انتهاء غير صالح", "rs256-v1")
        edition = str(payload.get("edition") or (edition_row[0] if edition_row else "standard"))
        return LicenseStatus(True, True, row["license_key"], edition, expires_at, device_id, None, "rs256-v1")

    def _post_activation(
        self,
        key: str,
        app_version: str = "",
        *,
        timeout: float = 15.0,
        revalidate: bool = False,
    ) -> dict:
        # Exact Hawaa Al-Sham request contract. app_version is intentionally not
        # sent because the existing Worker does not require it.
        body = json.dumps(
            {"licenseCode": key, "fingerprint": self.device_id()},
            ensure_ascii=False,
        ).encode("utf-8")
        version_token = "".join(
            ch for ch in (app_version or "") if ch.isalnum() or ch in ".-_"
        )[:32] or "unknown"
        request = urllib.request.Request(
            HAWAA_REVALIDATE_URL if revalidate else self.server_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Identify the native activation client explicitly instead of
                # relying on urllib's generic Python-urllib/<version> signature.
                "User-Agent": f"Nano-Offline/{version_token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if getattr(response, "status", 200) != 200:
                    raise _ActivationError(raw or "فشل التفعيل")
        except urllib.error.HTTPError as exc:
            try:
                message = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                message = ""

            # Cloudflare 1010 means the request was rejected by an owner-side
            # browser/client signature rule before the activation Worker could
            # process the license payload. Retrying the same request is useless,
            # but it is NOT a license decision, so the local license is kept.
            error_code = None
            error_name = ""
            if message:
                try:
                    error_payload = json.loads(message)
                except json.JSONDecodeError:
                    error_payload = None
                if isinstance(error_payload, dict):
                    error_code = error_payload.get("error_code")
                    error_name = str(error_payload.get("error_name") or "")
            signature_blocked = (
                str(error_code) == "1010"
                or error_name == "browser_signature_banned"
                or "browser_signature_banned" in message.lower()
                or "error 1010" in message.lower()
            )
            if exc.code == 403 and signature_blocked:
                raise _ActivationError(
                    "حظر Cloudflare عميل التفعيل (Error 1010). "
                    "يلزم تعديل إعدادات Cloudflare لسيرفر التفعيل للسماح بمسار /activate "
                    "أو إزالة قاعدة حظر User-Agent الخاصة بهذا العميل؛ إعادة المحاولة الآن لن تفيد."
                ) from exc

            # Prefer a concise API error when the server returned JSON instead of
            # surfacing the complete raw Cloudflare/server payload in the UI.
            if message:
                try:
                    error_payload = json.loads(message)
                except json.JSONDecodeError:
                    error_payload = None
                if isinstance(error_payload, dict):
                    concise = str(
                        error_payload.get("message")
                        or error_payload.get("detail")
                        or ""
                    ).strip()
                    if concise:
                        raise _ActivationError(
                            _WORKER_ERROR_MESSAGES.get(concise.lower(), concise),
                            rejected=exc.code in (400, 401, 403, 404),
                        ) from exc
            localized = _WORKER_ERROR_MESSAGES.get((message or "").strip().lower())
            raise _ActivationError(
                localized or message or f"رفض خادم التفعيل الطلب ({exc.code})",
                rejected=exc.code in (400, 401, 403, 404),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _ActivationError("تعذر الاتصال بخدمة التفعيل") from exc
        try:
            result = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise _ActivationError("استجابة سيرفر التفعيل غير مفهومة") from exc
        if not isinstance(result, dict):
            raise _ActivationError("استجابة سيرفر التفعيل غير صحيحة")
        if result.get("success") is False:
            raise _ActivationError(str(result.get("message") or "فشل التفعيل"), rejected=True)
        return result

    def activate_online(self, license_key: str, app_version: str = "", *, timeout: float = 15.0) -> LicenseStatus:
        key = (license_key or "").strip()
        if not key:
            raise ValueError("أدخل مفتاح الترخيص")
        try:
            result = self._post_activation(key, app_version, timeout=timeout)
        except _ActivationError as exc:
            raise RuntimeError(str(exc)) from None
        return self._install_hawaa_result(key, result)

    def revalidate(self, *, timeout: float = 10.0) -> LicenseStatus:
        """Silent online re-check for an already-activated device.

        - server accepts -> refresh the signed token and ``last_verified_at``
        - server rejects (revoked/disabled/expired/deleted) -> drop activation
        - offline/unreachable/bad signature -> keep the current license (grace)
        """
        local = self.status()
        if not local.activated:
            return local
        with self.db.connect() as conn:
            row = conn.execute("SELECT license_key FROM license_state WHERE id=1").fetchone()
        key = str(row["license_key"] or "").strip() if row else ""
        if not key:
            return local
        try:
            result = self._post_activation(key, "", timeout=timeout, revalidate=True)
        except _ActivationError as exc:
            if exc.rejected:
                self.clear_activation()
                return LicenseStatus(
                    True, False, key, None, local.expires_at, local.device_id,
                    str(exc), HAWAA_PROTOCOL_V2,
                )
            return local
        except Exception:
            return local
        try:
            return self._install_hawaa_result(key, result)
        except Exception:
            # Signature/integrity failure must never destroy a good local
            # license; a fake server cannot produce a valid signature anyway.
            return local

    def clear_activation(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM license_state")
            conn.execute("INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('deactivate','license',1,'local clear')")

    def safe_details(self) -> dict:
        status = self.status()
        key = status.license_key or ""
        return {
            "activated": status.activated,
            "valid": status.valid,
            "message": "الترخيص مفعل" if status.valid else (status.reason or "غير مفعل"),
            "device_id": status.device_id or self.device_id(),
            "expiration": status.expires_at or "غير محدود/غير محدد",
            "edition": status.edition or "standard",
            "protocol": status.protocol or HAWAA_PROTOCOL,
            "server": HAWAA_ACTIVATION_URL,
            "key_preview": ("****-" + key[-4:]) if len(key) >= 4 else ("****" if key else ""),
        }


__all__ = [
    "LicenseService",
    "LicenseStatus",
    "verify_rs256",
    "_b64url_encode",
    "HAWAA_ACTIVATION_URL",
    "HAWAA_REVALIDATE_URL",
    "HAWAA_PROTOCOL",
    "HAWAA_PROTOCOL_V2",
    "LICENSE_PUBLIC_KEY_N",
    "LICENSE_PUBLIC_KEY_E",
]
