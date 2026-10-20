from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from nano_offline.core import platform_capabilities

APP_DIR_NAME = "nano-offline"
PRIMARY_DB_NAME = "nano.db"
LEGACY_DB_NAME = "qeid.db"
_WINDOWS_MIGRATIONS_CHECKED: set[tuple[str, str]] = set()


def app_data_dir() -> Path:
    """Return the persistent writable application data directory.

    Flet exposes ``FLET_APP_STORAGE_DATA`` on packaged mobile apps.  Desktop
    development can override the location with ``NANO_DATA_DIR`` (with ``QEID_DATA_DIR`` retained for backward compatibility).  The fallback
    deliberately lives outside the source tree so packaged assets are never
    treated as writable data.
    """
    configured = (
        os.environ.get("FLET_APP_STORAGE_DATA")
        or os.environ.get("NANO_DATA_DIR")
        or os.environ.get("QEID_DATA_DIR")
        or ""
    ).strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        if platform_capabilities.IS_WINDOWS:
            path = platform_capabilities.app_data_dir()
            _migrate_legacy_windows_data(Path.home() / ".nano", path)
        else:
            path = Path.home() / ".nano"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _migrate_legacy_windows_data(source_dir: Path, target_dir: Path) -> None:
    """Copy legacy per-user data to the documented Windows location safely.

    The old directory is intentionally retained. SQLite's backup API includes
    committed WAL data; other files are copied only when the destination does
    not already contain them.
    """
    try:
        source_resolved = source_dir.resolve()
        target_resolved = target_dir.resolve()
    except OSError:
        source_resolved = source_dir
        target_resolved = target_dir
    key = (str(source_resolved), str(target_resolved))
    if source_resolved == target_resolved or key in _WINDOWS_MIGRATIONS_CHECKED or not source_dir.is_dir():
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    target_has_database = any((target_dir / name).exists() for name in (PRIMARY_DB_NAME, LEGACY_DB_NAME))
    if not target_has_database:
        legacy_db = next(
            (source_dir / name for name in (PRIMARY_DB_NAME, LEGACY_DB_NAME) if (source_dir / name).is_file()),
            None,
        )
        if legacy_db is not None:
            destination = target_dir / PRIMARY_DB_NAME
            temporary = destination.with_suffix(destination.suffix + ".migrating")
            source = sqlite3.connect(legacy_db)
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
                target.commit()
                integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            except Exception:
                target.close()
                source.close()
                temporary.unlink(missing_ok=True)
                raise
            else:
                target.close()
                source.close()
            if integrity.lower() != "ok":
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"فشل ترحيل قاعدة بيانات ويندوز القديمة: {integrity}")
            os.replace(temporary, destination)

    for directory_name in ("backups", "logs"):
        old_directory = source_dir / directory_name
        if not old_directory.is_dir():
            continue
        for old_path in old_directory.rglob("*"):
            if old_path.is_symlink() or not old_path.is_file() or old_path.name.endswith(".migrating"):
                continue
            relative = old_path.relative_to(source_dir)
            new_path = target_dir / relative
            if new_path.exists():
                continue
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_path, new_path)
    _WINDOWS_MIGRATIONS_CHECKED.add(key)


def migrate_legacy_database(legacy_path: str | Path, target_path: str | Path | None = None) -> bool:
    """One-time migration from the phase-1..5 source-tree database location.

    SQLite's backup API is used instead of a raw file copy so a legacy WAL
    database is migrated consistently. Existing target data is never replaced.
    """
    legacy = Path(legacy_path)
    target = Path(target_path) if target_path is not None else database_path()
    if target.exists() or not legacy.is_file() or legacy.resolve() == target.resolve():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".migrating")
    if temp.exists():
        temp.unlink()
    source = sqlite3.connect(legacy)
    destination = sqlite3.connect(temp)
    try:
        source.backup(destination)
        destination.commit()
        integrity = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"فشل ترحيل قاعدة البيانات القديمة: {integrity}")
    finally:
        destination.close()
        source.close()
    os.replace(temp, target)
    return True


def database_path() -> Path:
    base = app_data_dir()
    new_path = base / PRIMARY_DB_NAME
    legacy_path = base / LEGACY_DB_NAME
    if new_path.exists():
        return new_path
    if legacy_path.exists():
        return legacy_path
    return new_path


def backups_dir() -> Path:
    path = app_data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


_SHARED_DATA_DIR: Path | None = None


def apply_shared_data_dir(path: str | Path) -> Path:
    """Pin the suite database directory to an explicit (shared) location.

    Used on Android so multiple entry points (accounting / inventory / POS)
    — or a single app after reinstall — resolve the SAME nano.db. The value
    is kept in-process AND mirrored into NANO_DATA_DIR so later calls of
    app_data_dir()/database_path() (in any module) see it.
    """
    global _SHARED_DATA_DIR
    resolved = Path(path).expanduser()
    _SHARED_DATA_DIR = resolved
    os.environ["NANO_DATA_DIR"] = str(resolved)
    os.environ.pop("QEID_DATA_DIR", None)
    return resolved


def is_shared_data_dir_active() -> bool:
    return _SHARED_DATA_DIR is not None


def shared_data_dir() -> Path | None:
    return _SHARED_DATA_DIR


__all__ = [
    "app_data_dir",
    "database_path",
    "backups_dir",
    "migrate_legacy_database",
    "apply_shared_data_dir",
    "is_shared_data_dir_active",
    "shared_data_dir",
    "APP_DIR_NAME",
    "PRIMARY_DB_NAME",
    "LEGACY_DB_NAME",
]
