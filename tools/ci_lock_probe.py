"""TEMPORARY CI probe (see .github/workflows/debug-windows-locks.yml).

Reproduces the exact quality-gate failure pattern -- create a file/DB,
close everything, immediately unlink -- across four cases, and when a
file refuses deletion, asks handle.exe WHO holds it. Pure stdlib, no
project imports, so the result cannot blame app code paths.

Delete together with the debug workflow once diagnosed.
"""

from __future__ import annotations

import gc
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HANDLE = os.path.join(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()), "handle64.exe")


def who_holds(path: str) -> str:
    try:
        r = subprocess.run(
            [HANDLE, "-accepteula", "-nobanner", path],
            capture_output=True, text=True, timeout=60,
        )
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        return f"<handle.exe failed: {exc!r}>"
    lines = [ln for ln in out.splitlines() if "pid:" in ln.lower() or "no matching handles" in ln.lower()]
    return "\n".join(lines[:6]) or out[-800:]


def trial(name: str, make_close, rounds: int = 30) -> tuple[int, str]:
    """Returns (locked_count, first_locker_report)."""
    locked = 0
    locker = ""
    for _ in range(rounds):
        path = make_close()
        gc.collect()
        try:
            os.unlink(path)
        except PermissionError:
            locked += 1
            if not locker:
                locker = who_holds(path)
                # Leave the locked file for inspection, clean the rest.
                continue
        # also drop sidecars (-wal/-shm) if the case created any
        for suffix in ("-wal", "-shm", "-journal"):
            side = path + suffix
            if os.path.exists(side):
                try:
                    os.unlink(side)
                except OSError:
                    pass
    # sweep leftovers of locked rounds
    return locked, locker


def make_plain() -> str:
    fd, path = tempfile.mkstemp(prefix="probe-plain-", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write("x" * 1000)
    return path


def make_sqlite_delete() -> str:
    fd, path = tempfile.mkstemp(prefix="probe-sqlite-delete-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


def make_sqlite_wal() -> str:
    fd, path = tempfile.mkstemp(prefix="probe-sqlite-wal-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


def make_sqlite_wal_checkpoint() -> str:
    fd, path = tempfile.mkstemp(prefix="probe-sqlite-wal-ckpt-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    conn.close()
    return path


def main() -> None:
    print(f"python={sys.version.split()[0]} handle={os.path.exists(HANDLE)}", flush=True)
    total_locked = 0
    for name, maker in [
        ("plain-file", make_plain),
        ("sqlite-DELETE", make_sqlite_delete),
        ("sqlite-WAL", make_sqlite_wal),
        ("sqlite-WAL-checkpointed", make_sqlite_wal_checkpoint),
    ]:
        locked, locker = trial(name, maker)
        total_locked += locked
        print(f"CASE {name}: locked {locked}/30", flush=True)
        if locker:
            print(f"LOCKER for {name}:\n{locker}", flush=True)
    print(f"TOTAL LOCKED: {total_locked}", flush=True)
    if total_locked:
        raise SystemExit(1)
    print("ci_lock_probe: OK - no locks observed")


if __name__ == "__main__":
    main()
