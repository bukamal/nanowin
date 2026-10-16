"""Contract tests for the shared KPI strip.
"""
from __future__ import annotations

import flet as ft

from nano_offline.components.metric_row import metric_controls, metric_row


def test_cards_share_one_row_on_desktop():
    cards = metric_controls([ft.Container() for _ in range(4)])
    assert [c.col["md"] for c in cards] == [3, 3, 3, 3]  # 4 across
    assert [c.col["xs"] for c in cards] == [6, 6, 6, 6]  # 2 across on phones


def test_six_cards_fill_the_row_too():
    cards = metric_controls([ft.Container() for _ in range(6)])
    assert {c.col["md"] for c in cards} == {2}


def test_non_container_controls_are_wrapped():
    row = metric_row([ft.Text("a"), ft.Text("b")])
    assert isinstance(row, ft.ResponsiveRow)
    assert all(isinstance(c, ft.Container) for c in row.controls)
    assert {c.col["md"] for c in row.controls} == {6}
