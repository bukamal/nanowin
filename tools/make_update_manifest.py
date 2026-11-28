"""Write the ``update.json`` release manifest (optionally signed).

Usage (CI, after building the installer):
    python tools/make_update_manifest.py \
        --version 0.17.66 --url https://github.com/.../Nano-Setup-0.17.66.exe \
        --sha256 <hex> --size 123456 --filename Nano-Setup-0.17.66.exe \
        --notes "..." --out update.json [--signing-key <base64>]

The canonical signed string is ``v1|version|sha256|size|filename`` — exactly
what ``core.updater.UpdateInfo.signature_payload`` rebuilds on the client.
"""

from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime, timezone
from pathlib import Path


def canonical(version: str, sha256: str, size: int, filename: str) -> bytes:
    return f"v1|{version}|{sha256}|{size}|{filename}".encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--page-url", default="")
    parser.add_argument("--out", default="update.json")
    parser.add_argument("--signing-key", default="", help="base64 raw Ed25519 private key")
    args = parser.parse_args()

    sha = args.sha256.strip().lower()
    manifest = {
        "version": args.version.strip(),
        "url": args.url.strip(),
        "sha256": sha,
        "size": int(args.size),
        "filename": args.filename.strip(),
        "notes": args.notes,
        "page_url": args.page_url,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "mandatory": False,
    }
    if args.signing_key:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(args.signing_key))
        signature = key.sign(canonical(manifest["version"], sha, manifest["size"], manifest["filename"]))
        manifest["signature"] = base64.b64encode(signature).decode()

    Path(args.out).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out} (signed={bool(args.signing_key)})")


if __name__ == "__main__":
    main()