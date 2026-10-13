"""Platform capability flags for the Windows flavor (nanowin).

The whole app (views, services, repositories) is platform-agnostic and
shared with the Android tree. Only *native-device* features differ, and
every one of them already degrades through the ``native_files is None``
checks spread across the views. This module is the single place that
documents, per platform, what is native / what falls back / what is off,
so call sites never have to guess from ``sys.platform`` inline.

Dependency-free on purpose (no flet import): smoke tests and tools can
import it anywhere, including on machines without the UI stack.

Sync policy: this file is OWNED by nanowin. ``tools/sync_from_nano.sh``
excludes it when pulling the shared tree from the ``nano`` repo, so an
Android-side change can never silently overwrite the Windows matrix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS: bool = sys.platform.startswith("win")
IS_MACOS: bool = sys.platform == "darwin"
IS_ANDROID: bool = (
    sys.platform.startswith("linux")
    and ("ANDROID_ARGUMENT" in os.environ or "ANDROID_ROOT" in os.environ)
)
IS_DESKTOP: bool = IS_WINDOWS or IS_MACOS or (sys.platform.startswith("linux") and not IS_ANDROID)

# Feature matrix: "native" works through flet_native_files on that
# platform, "fallback" means the app already has a non-native path for it,
# "off" means the UI must hide/disable the entry point entirely.
_FEATURES: dict[str, dict[str, str]] = {
    # Same plugin packages serve both platforms, no work needed.
    "file_picker": {"android": "native", "windows": "native"},
    "file_share": {"android": "native", "windows": "native"},
    "print_html": {"android": "native", "windows": "native"},
    "create_pdf": {"android": "native", "windows": "native"},
    "notifications": {"android": "native", "windows": "native"},
    "sound_pool": {"android": "native", "windows": "native"},
    # No phone camera on a desktop: pick an image file instead. The wedge
    # (USB keyboard) barcode path needs no code at all and is already the
    # recommended desktop scanning mode (see barcode settings).
    "camera_capture": {"android": "native", "windows": "fallback:file-pick"},
    "barcode_camera_scan": {"android": "native", "windows": "fallback:wedge-and-image-decode"},
    # Android SpeechRecognizer has no desktop counterpart in the current
    # extension; voice entry points must stay hidden on Windows until a
    # local engine (Vosk/Whisper) is bridged.
    "speech_recognition": {"android": "native", "windows": "off"},
    # Android AppWidget has no desktop counterpart; the planned replacement
    # is a system-tray icon with today's totals.
    "home_widget": {"android": "native", "windows": "off:tray-planned"},
    # Not bridged on either platform yet (ESC/POS over USB/serial).
    "cash_drawer": {"android": "off:planned", "windows": "off:planned"},
}


def current_platform() -> str:
    """Return ``"windows"``, ``"android"``, ``"macos"`` or ``"other"``."""
    if IS_WINDOWS:
        return "windows"
    if IS_ANDROID:
        return "android"
    if IS_MACOS:
        return "macos"
    return "other"


def feature_status(feature: str) -> str:
    """Return the status string for ``feature`` on this platform.

    Unknown features return ``"off:unknown"`` (fail closed: the UI must
    hide the entry point rather than call into a missing bridge).
    """
    return _FEATURES.get(feature, {}).get(current_platform(), "off:unknown")


def supports(feature: str) -> bool:
    """True when ``feature`` works natively on this platform."""
    return feature_status(feature) == "native"


def app_data_dir(app_name: str = "Nano") -> Path:
    """Writable per-user data directory for databases/backups/exports.

    ``%APPDATA%\\<app>`` on Windows, ``~/.nano`` elsewhere (matches the
    historical default already used by ``core.paths`` on Android/desktop).
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / app_name
    return Path.home() / ".nano"


__all__ = [
    "IS_WINDOWS",
    "IS_MACOS",
    "IS_ANDROID",
    "IS_DESKTOP",
    "current_platform",
    "feature_status",
    "supports",
    "app_data_dir",
]
