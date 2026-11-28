"""Base64 Ed25519 public key used to verify release manifests.

Generate a key pair with ``python tools/generate_update_key.py``:

* put the **public** key here (it is safe to commit), and
* store the **private** key in the ``UPDATE_SIGNING_KEY`` CI secret.

When this stays empty, updates are still verified by SHA-256 over HTTPS; the
signature becomes an additional, stronger guarantee once configured.
"""

UPDATE_PUBLIC_KEY = ""

__all__ = ["UPDATE_PUBLIC_KEY"]