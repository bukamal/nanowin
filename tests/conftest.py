"""Shared pytest fixtures for the whole suite.

Windows note: SQLite database files cannot be deleted while any handle --
even one kept alive only by a reference cycle -- still references them,
so every temporary-database teardown starts with a cyclic collection.
On Linux this is a no-op; on Windows it is the difference between a
green suite and ``PermissionError: [WinError 32]`` in teardown.
"""

from __future__ import annotations

import gc

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item) -> None:  # noqa: ARG001 - hook signature fixed by pytest
    gc.collect()
