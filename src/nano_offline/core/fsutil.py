"""Windows-safe file operations.

Deleting or replacing a SQLite database file moments after its last
connection closed is unreliable on Windows: antivirus/indexer scans and
memory-map release timing can hold the file for a few hundred
milliseconds, surfacing as ``PermissionError: [WinError 32]``. Linux
never notices (unlink works on open files), so this only ever bites on
Windows CI and real user machines -- exactly where restores and test
teardown do file surgery.

The helpers below retry *only* ``PermissionError`` with exponential
backoff (first attempt is immediate, so the Linux/happy path costs
nothing extra) and re-raise the original error when the window is
exhausted. Every other exception propagates instantly -- retries must
never mask a real logic error.
"""

from __future__ import annotations

import gc
import os
import shutil
import tempfile
import time
from pathlib import Path

_RETRIES = 9
_FIRST_DELAY = 0.05


def _retry_permission_error(op, *args):
    """Run ``op(*args)``; retry solely on PermissionError with backoff."""
    delay = _FIRST_DELAY
    error: OSError | None = None
    for _ in range(_RETRIES):
        try:
            return op(*args)
        except PermissionError as exc:
            error = exc
            time.sleep(delay)
            delay *= 2
    assert error is not None
    raise error


def unlink_retry(path: str | Path) -> None:
    """Unlink ``path``; tolerate the Windows lock window, then give up."""
    _retry_permission_error(os.unlink, os.fspath(path))


def replace_retry(src: str | Path, dst: str | Path) -> None:
    """Atomic ``os.replace`` with the same transient-lock tolerance."""
    _retry_permission_error(os.replace, os.fspath(src), os.fspath(dst))


def clean_tmpdir(tmp: tempfile.TemporaryDirectory) -> None:
    """Drop-in replacement for ``tmp.cleanup()`` in tests.

    Runs a cyclic collection first (a connection kept alive only by a
    reference cycle still locks its database on Windows until the
    collector frees it), then removes the tree tolerating transient
    external locks the same way :func:`unlink_retry` does.
    """
    gc.collect()

    def _rmtree() -> None:
        shutil.rmtree(tmp.name, ignore_errors=False)

    _retry_permission_error(_rmtree)


__all__ = ["unlink_retry", "replace_retry", "clean_tmpdir"]
