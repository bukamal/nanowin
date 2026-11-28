import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nano_offline.core import updater


def _keypair():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return key, base64.b64encode(pub).decode()


class _Resp:
    def __init__(self, data: bytes):
        self._d = data
        self._i = 0
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        chunk = self._d[self._i:self._i + (n if n and n > 0 else len(self._d))]
        self._i += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_version_compare():
    assert updater.version_tuple("v0.17.65") == (0, 17, 65)
    assert updater.is_newer("0.17.66", "0.17.65")
    assert not updater.is_newer("0.17.65", "0.17.65")
    assert updater.is_newer("1.0.0", "0.99.99")


def test_parse_manifest_requires_sha256():
    with_sha = {
        "version": "0.18.0", "url": "https://x/a.exe",
        "sha256": "a" * 64, "size": 10, "filename": "a.exe",
    }
    info = updater.parse_manifest(with_sha)
    assert info.version == "0.18.0"
    assert info.size == 10
    for bad in (
        {"version": "", "url": "u", "sha256": "a" * 64},
        {"version": "1", "url": "", "sha256": "a" * 64},
        {"version": "1", "url": "u", "sha256": "short"},
    ):
        try:
            updater.parse_manifest(bad)
        except updater.UpdaterError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected failure for {bad}")


def test_signature_roundtrip():
    key, pub = _keypair()
    payload = b"v1|1.0.0|abc|10|a.exe"
    sig = base64.b64encode(key.sign(payload)).decode()
    assert updater.verify_signature(payload, sig, pub)
    assert not updater.verify_signature(payload + b"x", sig, pub)


def test_verify_update_checks_hash_and_signature(tmp_path: Path):
    data = b"installer-bytes"
    path = tmp_path / "a.exe"
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    key, pub = _keypair()
    info = updater.UpdateInfo(
        version="0.18.0", download_url="https://x/a.exe", sha256=sha,
        size=len(data), filename="a.exe",
    )
    assert updater.verify_update(path, info)
    # wrong hash
    bad = updater.UpdateInfo(version="0.18.0", download_url="u", sha256="0" * 64, size=len(data), filename="a.exe")
    assert not updater.verify_update(path, bad)
    # signed manifest
    signed = updater.UpdateInfo(
        version="0.18.0", download_url="u", sha256=sha, size=len(data),
        filename="a.exe", signature=base64.b64encode(key.sign(info.signature_payload)).decode(),
    )
    assert updater.verify_update(path, signed, public_key_b64=pub)
    # tampered signature rejected
    forged = updater.UpdateInfo(**{**signed.__dict__, "signature": base64.b64encode(b"x" * 64).decode()})
    assert not updater.verify_update(path, forged, public_key_b64=pub)


def test_download_update_streams_with_progress(tmp_path: Path):
    payload = b"Z" * 5000
    info = updater.UpdateInfo(version="0.18.0", download_url="https://x/a.exe",
                              sha256=hashlib.sha256(payload).hexdigest(), size=len(payload), filename="a.exe")
    seen: list[tuple[int, int]] = []

    def opener(_request, timeout=0):
        return _Resp(payload)

    out = updater.download_update(info, tmp_path, on_progress=lambda d, t: seen.append((d, t)), opener=opener)
    assert out.read_bytes() == payload
    assert seen and seen[-1][0] == len(payload)


def test_check_for_update_reads_manifest():
    installer = b"X" * 20
    sha = hashlib.sha256(installer).hexdigest()
    release = {
        "tag_name": "v0.18.0",
        "html_url": "https://github.com/bukamal/nanowin/releases/tag/v0.18.0",
        "assets": [
            {"name": "update.json", "browser_download_url": "https://x/update.json"},
            {"name": "Nano-Setup-0.18.0.exe", "browser_download_url": "https://x/Nano-Setup-0.18.0.exe"},
        ],
    }
    manifest = {
        "version": "0.18.0", "url": "https://x/Nano-Setup-0.18.0.exe",
        "sha256": sha, "size": len(installer), "filename": "Nano-Setup-0.18.0.exe",
        "notes": "جديد",
    }

    def opener(request, timeout=0):
        url = request.full_url
        if url.endswith("/releases/latest"):
            return _Resp(json.dumps(release).encode())
        if url.endswith("update.json"):
            return _Resp(json.dumps(manifest).encode())
        raise AssertionError(url)

    info = updater.check_for_update("0.17.65", opener=opener)
    assert info is not None and info.version == "0.18.0"
    assert info.sha256 == sha
    assert updater.check_for_update("0.18.0", opener=opener) is None