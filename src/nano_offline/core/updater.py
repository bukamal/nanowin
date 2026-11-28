"""Secure in-app updater for the Windows build.

Flow (all in this module, free of ``flet`` so it is unit-testable):

1. :func:`check_for_update` reads the latest GitHub *Release* for the repo,
   downloads its small ``update.json`` manifest and compares versions.
2. :func:`download_update` streams the installer to the app-data dir with a
   progress callback (same UX as restore).
3. :func:`verify_update` checks the SHA-256 from the manifest and, when a
   signature is present and a public key is configured, an Ed25519 signature
   over a canonical ``v1|version|sha256|size|filename`` string.
4. :func:`install_update` launches the verified Inno Setup installer silently.

Nothing is installed unless verification passes. The manifest is fetched over
HTTPS from the official repository only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

GITHUB_REPO = "bukamal/nanowin"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
MANIFEST_ASSET = "update.json"
USER_AGENT = "NanoUpdater/1.0 (+https://github.com/bukamal/nanowin)"

# Base64 of a raw 32-byte Ed25519 public key, baked into the build (committed
# module or a CI-baked file). When empty, updates are still protected by the
# manifest SHA-256 (fetched over HTTPS from the official release), but
# signature checking is skipped.
def _load_public_key() -> str:
    env = os.environ.get("NANO_UPDATE_PUBLIC_KEY", "").strip()
    if env:
        return env
    try:
        from nano_offline.core.update_public_key import UPDATE_PUBLIC_KEY as file_key

        return str(file_key or "").strip()
    except Exception:  # noqa: BLE001
        return ""


UPDATE_PUBLIC_KEY = _load_public_key()

_CHUNK = 256 * 1024


class UpdaterError(Exception):
    """A user-facing updater failure (offline, bad manifest, etc.)."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    download_url: str
    sha256: str
    size: int = 0
    filename: str = ""
    notes: str = ""
    published_at: str = ""
    signature: str = ""
    page_url: str = ""
    mandatory: bool = False

    @property
    def signature_payload(self) -> bytes:
        return f"v1|{self.version}|{self.sha256}|{self.size}|{self.filename}".encode("utf-8")


def version_tuple(value: str) -> tuple[int, ...]:
    """Parse ``v0.17.65`` / ``0.17.65`` into a comparable integer tuple."""
    text = str(value or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = version_tuple(latest), version_tuple(current)
    length = max(len(a), len(b))
    a = a + (0,) * (length - len(a))
    b = b + (0,) * (length - len(b))
    return a > b


def _request(url: str, *, timeout: float, accept: str | None = None) -> Request:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return Request(url, headers=headers)


def _fetch_bytes(url: str, *, timeout: float = 20.0, accept: str | None = None, opener=None) -> bytes:
    open_fn = opener or urlopen
    try:
        with open_fn(_request(url, timeout=timeout, accept=accept), timeout=timeout) as response:
            return response.read()
    except Exception as exc:  # noqa: BLE001 - network errors are all user-facing
        raise UpdaterError(f"تعذّر الاتصال بخادم التحديثات: {exc}") from exc


def fetch_latest_release(*, timeout: float = 20.0, opener=None) -> dict:
    raw = _fetch_bytes(LATEST_RELEASE_API, timeout=timeout, accept="application/vnd.github+json", opener=opener)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise UpdaterError("استجابة GitHub غير صالحة") from exc


def parse_manifest(data: dict) -> UpdateInfo:
    try:
        version = str(data.get("version") or "").strip()
        download_url = str(data.get("url") or data.get("download_url") or "").strip()
        sha256 = str(data.get("sha256") or "").strip().lower()
    except Exception as exc:  # noqa: BLE001
        raise UpdaterError("بيانات التحديث غير صالحة") from exc
    if not version or not download_url or len(sha256) != 64:
        raise UpdaterError("بيانات التحديث ناقصة (الإصدار/الرابط/البصمة)")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return UpdateInfo(
        version=version,
        download_url=download_url,
        sha256=sha256,
        size=size,
        filename=str(data.get("filename") or Path(download_url).name or f"Nano-Setup-{version}.exe"),
        notes=str(data.get("notes") or ""),
        published_at=str(data.get("published_at") or ""),
        signature=str(data.get("signature") or ""),
        page_url=str(data.get("page_url") or ""),
        mandatory=bool(data.get("mandatory") or False),
    )


def check_for_update(current_version: str, *, timeout: float = 20.0, opener=None) -> UpdateInfo | None:
    """Return the newer release manifest, or ``None`` when up to date.

    Raises :class:`UpdaterError` for network/format problems so the UI can
    show a clear message instead of a silent no-op.
    """
    release = fetch_latest_release(timeout=timeout, opener=opener)
    assets = release.get("assets") or []
    manifest_url = None
    fallback_url = None
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name == MANIFEST_ASSET:
            manifest_url = url
            break
        if not fallback_url and name.lower().endswith(".exe"):
            fallback_url = url
    if not manifest_url:
        # A release without a manifest can't be verified; if it is older we're
        # simply up to date, otherwise tell the user the release is malformed.
        if is_newer(str(release.get("tag_name") or ""), current_version):
            raise UpdaterError("الإصدار الجديد لا يحتوي ملف التحقق update.json")
        return None
    manifest = json.loads(_fetch_bytes(manifest_url, timeout=timeout, opener=opener).decode("utf-8"))
    info = parse_manifest(manifest)
    if not info.page_url:
        info = UpdateInfo(**{**info.__dict__, "page_url": str(release.get("html_url") or "")})
    if not is_newer(info.version, current_version):
        return None
    return info


def sha256_file(path: Path, *, chunk_size: int = _CHUNK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_signature(payload: bytes, signature_b64: str, public_key_b64: str) -> bool:
    if not signature_b64 or not public_key_b64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:  # noqa: BLE001
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), payload)
        return True
    except Exception:  # noqa: BLE001
        return False


def verify_update(path: Path, info: UpdateInfo, *, public_key_b64: str | None = None) -> bool:
    path = Path(path)
    if not path.is_file():
        return False
    if info.size and path.stat().st_size != info.size:
        return False
    if sha256_file(path) != info.sha256.lower():
        return False
    key = public_key_b64 if public_key_b64 is not None else UPDATE_PUBLIC_KEY
    if info.signature and key:
        return verify_signature(info.signature_payload, info.signature, key)
    return True


def download_update(
    info: UpdateInfo,
    dest_dir: Path | str,
    *,
    on_progress=None,
    cancel=None,
    timeout: float = 60.0,
    opener=None,
) -> Path:
    """Stream the installer into ``dest_dir``; return the finished path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (info.filename or "update.exe")
    partial = target.with_suffix(target.suffix + ".part")
    open_fn = opener or urlopen
    total = info.size or 0
    done = 0
    try:
        with open_fn(_request(info.download_url, timeout=timeout), timeout=timeout) as response, open(partial, "wb") as out:
            length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            if length and not total:
                try:
                    total = int(length)
                except (TypeError, ValueError):
                    total = 0
            if on_progress:
                on_progress(done, total)
            while True:
                if cancel is not None and cancel():
                    raise UpdaterError("أُلغي التنزيل")
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    except UpdaterError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        partial.unlink(missing_ok=True)
        raise UpdaterError(f"فشل التنزيل: {exc}") from exc
    if partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise UpdaterError("الملف المُنزَّل فارغ")
    partial.replace(target)
    return target


def install_update(installer_path: Path | str) -> None:
    """Launch the verified Inno installer silently and let it replace the app.

    Inno's ``/CLOSEAPPLICATIONS`` closes this running instance; its post-install
    step (see windows/installer.iss) relaunches the app afterwards.
    """
    path = str(installer_path)
    if not Path(path).is_file():
        raise UpdaterError("ملف التحديث غير موجود")
    if not sys.platform.startswith("win"):
        raise UpdaterError("التثبيت الصامت مدعوم على Windows فقط")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        subprocess.Popen(
            [path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
            close_fds=True,
            creationflags=flags,
        )
    except Exception as exc:  # noqa: BLE001
        raise UpdaterError(f"تعذّر تشغيل المثبّت: {exc}") from exc


__all__ = [
    "GITHUB_REPO",
    "UPDATE_PUBLIC_KEY",
    "UpdateInfo",
    "UpdaterError",
    "version_tuple",
    "is_newer",
    "fetch_latest_release",
    "parse_manifest",
    "check_for_update",
    "sha256_file",
    "verify_signature",
    "verify_update",
    "download_update",
    "install_update",
]