"""Store-scene data model + renderer smoke tests.

``build_scene_model`` is deliberately Flet-free so it runs everywhere;
``test_render_returns_canvas_scene`` is skipped unless the pinned Flet build
exposes the canvas API (it does in CI's flet==0.28.3).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import flet as ft

from nano_offline.components import store_scene


def _ctx(*, summary=None, today=None, weekly=None, last_close=None):
    summary = {"inventory_value": 1000, "purchases": 200, "cash": 300, "receivables": 50, "net_profit": 400, **(summary or {})}
    today = {"total": 77, **(today or {})}
    weekly = {"low_stock_count": 3, **(weekly or {})}
    dashboard = SimpleNamespace(
        summary=lambda: summary,
        today_summary=lambda: today,
        weekly_owner_summary=lambda: weekly,
    )
    cash = SimpleNamespace(last_close=lambda: last_close)
    return SimpleNamespace(dashboard=dashboard, cash_day_close=cash)


def _fmt(value) -> str:
    return f"{float(value or 0):.0f}"


def test_zones_metrics_and_alerts():
    zones = store_scene.build_scene_model(_ctx(), fmt=_fmt, can=lambda _perm: True)
    assert [z.key for z in zones] == ["pos", "items", "purchase", "finance", "customers", "reports"]
    by = {z.key: z for z in zones}

    assert by["pos"].featured is True
    assert by["pos"].metric_value == "77"  # today's sales
    assert by["items"].metric_value == "1000"
    assert by["items"].alert is True  # low_stock_count == 3
    assert by["customers"].alert is True  # receivables > 0
    assert by["finance"].alert is True  # no day-close recorded today


def test_finance_not_alerting_once_day_is_closed():
    closed = {"date": date.today().isoformat()}
    zones = store_scene.build_scene_model(_ctx(last_close=closed), fmt=_fmt, can=lambda _perm: True)
    finance = next(z for z in zones if z.key == "finance")
    assert finance.alert is False


def test_permissions_filter_zones():
    zones = store_scene.build_scene_model(_ctx(), fmt=_fmt, can=lambda perm: perm == "items")
    assert [z.key for z in zones] == ["items"]


def test_zones_expose_quick_actions():
    zones = store_scene.build_scene_model(_ctx(), fmt=_fmt, can=lambda _perm: True)
    by = {z.key: z for z in zones}
    assert [a.id for a in by["pos"].actions] == ["new_sale", "last_sales"]
    assert [a.id for a in by["finance"].actions] == ["receipt", "payment"]


def test_render_returns_stack_with_canvas_shapes():
    if not store_scene._CANVAS_OK:
        import pytest

        pytest.skip("canvas API unavailable in this Flet build")

    class FakePage:
        width = 1280
        height = 720

        def update(self, *args, **kwargs):
            pass

        def run_task(self, *args, **kwargs):
            return None

    surface = store_scene.render_store_scene(
        FakePage(),
        _ctx(),
        fmt=_fmt,
        on_select=lambda _key: None,
        can=lambda _perm: True,
    )
    assert isinstance(surface, ft.Stack)
    canvas = next(control for control in surface.controls if hasattr(control, "shapes"))
    assert len(canvas.shapes) > 0
