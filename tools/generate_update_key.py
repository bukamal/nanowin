"""Generate an Ed25519 key pair for signing release manifests.

Usage:
    python tools/generate_update_key.py

Then:
  * commit the PUBLIC key into src/nano_offline/core/update_public_key.py
  * store the PRIVATE key as the UPDATE_SIGNING_KEY GitHub Actions secret
    (and optionally UPDATE_PUBLIC_KEY as a secret too, so CI bakes it).
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    key = Ed25519PrivateKey.generate()
    private_raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print("UPDATE_SIGNING_KEY (private, keep secret):")
    print(base64.b64encode(private_raw).decode())
    print()
    print("UPDATE_PUBLIC_KEY (public, safe to commit):")
    print(base64.b64encode(public_raw).decode())


if __name__ == "__main__":
    main()