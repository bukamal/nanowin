from __future__ import annotations

"""Device-local preferences, kept out of the shared database.

In client/server mode the ``settings`` table is shared (business config lives
on the central server), but some choices belong to *this device* -- theme,
print profile, dashboard layout, sound. This module classifies those keys and
stores them in a small JSON file next to the database (the same pattern auth
uses for ``login_prefs.json``), so a client can read shared settings from the
server and device preferences locally.

Phase 3 ships the classification + store as tested infrastructure; views keep
reading the shared table until client mode is switched on.
"""

import json
import threading
from pathlib import Path

#: Keys that are clearly this-device/session choices (exact names).
_EXPLICIT_DEVICE_KEYS = frozenset({
    "pos_auto_print",
    "pos_scan_feedback",
    "sound_enabled",
    "sound_volume",
    "sound_scheme",
})

#: Prefixes that mark a whole family of device-local keys.
_DEVICE_PREFIXES = ("theme_", "print_layout", "print_roll_width", "ui.dashboard_")


def is_device_local(key: str) -> bool:
    """Whether ``key`` is a device-local preference (not shared business config)."""
    text = str(key or "")
    if text in _EXPLICIT_DEVICE_KEYS:
        return True
    return text.startswith(_DEVICE_PREFIXES)


class LocalPrefs:
    """Tiny JSON-backed store for device-local preferences."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, key: str, default: str = "") -> str:
        return str(self._read().get(str(key), default))

    def get_all(self) -> dict:
        return {str(k): str(v) for k, v in self._read().items()}

    def set(self, key: str, value) -> None:
        self.set_many({key: value})

    def set_many(self, values: dict) -> None:
        with self._lock:
            data = self._read()
            for key, value in values.items():
                key = str(key)
                if value is None or value == "":
                    data.pop(key, None)
                else:
                    data[key] = str(value)
            self._write(data)


__all__ = ["is_device_local", "LocalPrefs"]
