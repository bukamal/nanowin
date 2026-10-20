from __future__ import annotations

import sqlite3
from pathlib import Path

from nano_offline.core import platform_capabilities
from nano_offline.core.paths import app_data_dir


def _create_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES(?)", (value,))


def test_windows_default_path_migrates_legacy_data_without_removing_source(tmp_path, monkeypatch):
    home = tmp_path / "home"
    appdata = tmp_path / "roaming"
    legacy_dir = home / ".nano"
    _create_db(legacy_dir / "nano.db", "legacy")
    (legacy_dir / "backups").mkdir(parents=True)
    (legacy_dir / "backups" / "old.nanobackup").write_bytes(b"backup")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    monkeypatch.delenv("NANO_DATA_DIR", raising=False)
    monkeypatch.delenv("QEID_DATA_DIR", raising=False)
    monkeypatch.setattr(platform_capabilities, "IS_WINDOWS", True)

    data_dir = app_data_dir()

    assert data_dir == appdata / "Nano"
    with sqlite3.connect(data_dir / "nano.db") as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "legacy"
    assert (data_dir / "backups" / "old.nanobackup").read_bytes() == b"backup"
    assert (legacy_dir / "nano.db").is_file()


def test_windows_migration_never_overwrites_existing_data(tmp_path, monkeypatch):
    home = tmp_path / "home"
    appdata = tmp_path / "roaming"
    _create_db(home / ".nano" / "nano.db", "legacy")
    _create_db(appdata / "Nano" / "nano.db", "current")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    monkeypatch.delenv("NANO_DATA_DIR", raising=False)
    monkeypatch.delenv("QEID_DATA_DIR", raising=False)
    monkeypatch.setattr(platform_capabilities, "IS_WINDOWS", True)

    data_dir = app_data_dir()

    with sqlite3.connect(data_dir / "nano.db") as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "current"


def test_explicit_data_directory_takes_precedence_on_windows(tmp_path, monkeypatch):
    home = tmp_path / "home"
    custom_dir = tmp_path / "custom-data"
    _create_db(home / ".nano" / "nano.db", "legacy")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NANO_DATA_DIR", str(custom_dir))
    monkeypatch.setattr(platform_capabilities, "IS_WINDOWS", True)

    assert app_data_dir() == custom_dir
    assert not (custom_dir / "nano.db").exists()
