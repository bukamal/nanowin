"""SettingsRepository read cache.

Regression guard for the "app gets slow with ~250 items" report: money
formatting used to open a fresh SQLite connection for every settings key it
read (thousands per screen rebuild). The settings table is now cached.
"""

from __future__ import annotations


def _repo(tmp_path):
    from nano_offline.core.database import Database
    from nano_offline.repositories.settings_repository import SettingsRepository

    db = Database(tmp_path / "settings.db")
    db.initialize()
    return db, SettingsRepository(db)


def test_repeated_reads_hit_the_cache_not_sqlite(tmp_path):
    db, repo = _repo(tmp_path)
    repo.set("exchange_rate_syp_per_usd", "13500")
    repo.get("exchange_rate_syp_per_usd")  # warm the cache

    calls = {"n": 0}
    original = db.connect

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    db.connect = counting
    try:
        for _ in range(200):
            assert repo.get("exchange_rate_syp_per_usd") == "13500"
    finally:
        db.connect = original
    assert calls["n"] == 0


def test_set_updates_the_cache_immediately(tmp_path):
    db, repo = _repo(tmp_path)
    repo.get("a")  # load cache
    repo.set("a", "1")
    assert repo.get("a") == "1"  # no reload/query needed
    repo.set("a", None)
    assert repo.get("a", "fallback") == "fallback"


def test_ttl_reload_sees_direct_writes(tmp_path):
    db, repo = _repo(tmp_path)
    repo.get("x")  # load cache
    with db.transaction() as conn:  # bypass the repository (as license/day-close do)
        conn.execute("INSERT INTO settings(key,value) VALUES('x','direct') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    repo.invalidate()
    assert repo.get("x") == "direct"