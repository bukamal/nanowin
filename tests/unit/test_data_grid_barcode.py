"""Keyboard-wedge barcode capture in DataGrid (no visible field)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from nano_offline.components.data_grid import DataGrid, GridColumn


def _grid(calls: list[str], *, accept: bool = True):
    def on_barcode(code: str) -> bool:
        calls.append(code)
        return accept

    return DataGrid(
        columns=[GridColumn("item", "المادة", editable=True)],
        rows=[{"item": 0}],
        on_barcode=on_barcode,
    )


def _key(ch: str):
    return SimpleNamespace(character=ch, key=ch, ctrl=False, alt=False, meta=False, shift=False)


def test_fast_burst_then_enter_is_captured_as_a_barcode():
    calls: list[str] = []
    grid = _grid(calls)

    # A scanner types the digits back-to-back, then Enter.
    for ch in "123456":
        assert grid._maybe_barcode_event(_key(ch), ch) is True
    assert grid._maybe_barcode_event(_key("Enter"), "enter") is True

    assert calls == ["123456"]


def test_slow_single_key_is_not_treated_as_a_barcode():
    calls: list[str] = []
    grid = _grid(calls)

    grid._maybe_barcode_event(_key("5"), "5")
    time.sleep(0.1)  # far slower than a scanner between keys
    # A terminator with a one-char buffer must NOT call the barcode callback.
    grid._maybe_barcode_event(_key("Enter"), "enter")

    assert calls == []


def test_callback_rejection_still_consumes_the_burst():
    calls: list[str] = []
    grid = _grid(calls, accept=False)

    for ch in "999":
        grid._maybe_barcode_event(_key(ch), ch)
    consumed = grid._maybe_barcode_event(_key("Enter"), "enter")

    assert calls == ["999"]  # callback saw it...
    assert consumed is True  # ...and the burst never leaked into a cell


def test_short_pending_key_does_not_open_a_burst():
    calls: list[str] = []
    grid = _grid(calls)

    # Two keys with a human-paced gap: first is held, second flushes to edit
    # (no barcode callback because it never reached the minimum length).
    grid._maybe_barcode_event(_key("7"), "7")
    time.sleep(0.1)
    grid._maybe_barcode_event(_key("8"), "8")

    assert calls == []
