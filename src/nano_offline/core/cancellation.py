"""Cooperative cancellation for long-running maintenance operations.

Backup restore can legitimately take minutes on large databases. Every
long loop (file copy, ZIP extraction, hashing, accounting rebuild) checks a
``threading.Event`` and raises :class:`OperationCancelled` so the UI can stop
*before* the live database is swapped. The caller decides where cancellation
is still safe; after the swap stage the event is ignored.
"""

from __future__ import annotations


class OperationCancelled(Exception):
    """Raised when the user asks a long operation to stop."""

    def __init__(self, message: str = "تم إلغاء العملية") -> None:
        super().__init__(message)


def check_cancel(cancel) -> None:
    """Raise :class:`OperationCancelled` if ``cancel`` (a threading.Event) is set."""
    if cancel is not None and cancel.is_set():
        raise OperationCancelled()


__all__ = ["OperationCancelled", "check_cancel"]
