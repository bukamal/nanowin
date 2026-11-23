#!/usr/bin/env python3
"""Migrate activation licenses in the Cloudflare LICENSES KV to schema v2.

The activation Worker (cloudflare/license-worker/worker.js) reads/writes a
richer per-license record so codes can be listed, disabled, extended and have
devices reset. This script upgrades the existing records in place (by default
every key already present in the namespace) without changing the ``/activate``
response contract. Real license codes are never hardcoded here — pass them with
``--codes`` or ``CF_LEGACY_CODES`` if you need to seed the index.

Read-only unless ``--apply`` is passed. Credentials come from the environment:

    export CF_API_TOKEN=...
    export CF_ACCOUNT_ID=8b169eee0400373c56faa58db7f3eae4
    python tools/migrate_license_kv.py                 # dry run (preview)
    python tools/migrate_license_kv.py --apply         # write the upgrades

Never commit a real token; pass it through the environment only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_NAMESPACE = "9d830730022f40e0b60d0f060987f533"
SCHEMA_VERSION = 2
DEFAULT_MAX_DEVICES = 330
INDEX_KEY = "__index__"
#: Optional operator seed (comma-separated) folded into the KV index. Kept in
#: the environment — never hardcode a real license code in this file.
LEGACY_CODES_ENV = "CF_LEGACY_CODES"


def _api(token: str, path: str, *, method: str = "GET", body=None) -> bytes:
    url = f"https://api.cloudflare.com/client/v4/{path.lstrip('/')}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _namespace(account: str, namespace: str, suffix: str) -> str:
    return f"accounts/{account}/storage/kv/namespaces/{namespace}/{suffix}"


def list_keys(token: str, account: str, namespace: str) -> list[str]:
    raw = _api(token, _namespace(account, namespace, "keys?limit=1000"))
    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(payload.get("errors") or "failed to list KV keys")
    return [str(item["name"]) for item in payload.get("result") or []]


def get_value(token: str, account: str, namespace: str, key: str):
    try:
        raw = _api(token, _namespace(account, namespace, f"values/{key}"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # key not set yet
            return None
        raise
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def put_value(token: str, account: str, namespace: str, key: str, value) -> None:
    _api(
        token,
        _namespace(account, namespace, f"values/{key}"),
        method="PUT",
        body=value,
    )


def max_devices_for(code: str, raw) -> int:
    if isinstance(raw, dict) and isinstance(raw.get("max_devices"), int):
        return raw["max_devices"]
    return DEFAULT_MAX_DEVICES


def env_legacy_codes() -> list[str]:
    return [c.strip() for c in os.environ.get(LEGACY_CODES_ENV, "").split(",") if c.strip()]


def upgrade_record(code: str, raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    devices = source.get("devices") if isinstance(source.get("devices"), list) else []
    clean_devices = [
        {
            "fingerprint": d.get("fingerprint"),
            "firstSeen": d.get("firstSeen"),
            "lastSeen": d.get("lastSeen", d.get("firstSeen")),
        }
        for d in devices
        if isinstance(d, dict) and isinstance(d.get("fingerprint"), str)
    ]
    first_seen = clean_devices[0]["firstSeen"] if clean_devices else None
    return {
        "schema_version": SCHEMA_VERSION,
        "code": code,
        "edition": source.get("edition") or "standard",
        "features": source.get("features") if isinstance(source.get("features"), list) else [],
        "max_devices": max_devices_for(code, source),
        "status": source.get("status") or "active",
        "expires_at": source.get("expires_at"),
        "notes": source.get("notes") or "",
        "created_at": source.get("created_at") or source.get("firstActivated") or first_seen,
        "firstActivated": source.get("firstActivated") or first_seen,
        "lastActivated": source.get("lastActivated"),
        "totalActivations": source.get("totalActivations") or 0,
        "activationAttempts": source.get("activationAttempts")
        if source.get("activationAttempts") is not None
        else (source.get("totalActivations") or 0),
        "devices": clean_devices,
    }


def _summary(code: str, before, after: dict) -> str:
    old = before if isinstance(before, dict) else {}
    already = old.get("schema_version") == SCHEMA_VERSION
    return (
        f"  {code}: devices {len(old.get('devices') or [])} -> {len(after['devices'])}, "
        f"max_devices={after['max_devices']}, activations={after['totalActivations']}, "
        f"status={after['status']}"
        + (" (already v2)" if already else " (will upgrade)")
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate LICENSES KV records to schema v2")
    parser.add_argument(
        "--codes",
        nargs="*",
        default=None,
        help="license codes to migrate (default: every existing KV key)",
    )
    parser.add_argument("--apply", action="store_true", help="write the upgrades (default: dry run)")
    parser.add_argument("--namespace", default=os.environ.get("CF_KV_NAMESPACE", DEFAULT_NAMESPACE))
    parser.add_argument("--write-index", action="store_true", help="also persist the KV code index")
    args = parser.parse_args(argv)

    token = os.environ.get("CF_API_TOKEN")
    account = os.environ.get("CF_ACCOUNT_ID")
    if not token or not account:
        print("error: set CF_API_TOKEN and CF_ACCOUNT_ID in the environment", file=sys.stderr)
        return 2

    existing = [k for k in list_keys(token, account, args.namespace) if k != INDEX_KEY]
    codes = [c for c in (args.codes if args.codes else existing) if c != INDEX_KEY]
    print(f"namespace: {args.namespace}")
    print(f"mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"existing keys: {existing}")

    upgraded = []
    for code in codes:
        before = get_value(token, account, args.namespace, code)
        after = upgrade_record(code, before)
        print(_summary(code, before, after))
        if args.apply:
            put_value(token, account, args.namespace, code, after)
            print(f"  -> wrote {code}")
        upgraded.append(code)

    if args.apply and args.write_index:
        # Union of the optional env seed, existing keys and the codes handled
        # above — so a deploy without the (removed) hardcoded list still knows
        # every license.
        index = sorted(
            set(env_legacy_codes())
            | set(existing)
            | set(codes)
            | set(upgraded)
        )
        index = [c for c in index if c != INDEX_KEY]
        put_value(token, account, args.namespace, INDEX_KEY, index)
        print(f"wrote index: {index}")

    if not args.apply:
        print("\ndry run only — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
