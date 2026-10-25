"""Dashboard hero row: equal card widths and RTL currency placement."""

from __future__ import annotations

from types import SimpleNamespace

import flet as ft

from nano_offline.views.dashboard_view import DashboardCenter


class FakeSettings:
    def get(self, key: str, default: str = "") -> str:
        return default


class FakePage:
    width = 1280
    height = 800
    rtl = True

    def update(self, *args, **kwargs) -> None:  # noqa: ARG002
        pass


def test_hero_row_places_currency_first_for_rtl_and_uses_equal_widths():
    dashboard = DashboardCenter(
        FakePage(),
        SimpleNamespace(settings=FakeSettings()),
        ft.Container(),
        on_navigate=lambda _route: None,
        on_open_sale=lambda: None,
        on_open_purchase=lambda: None,
    )
    pulse = ft.Container(ft.Text("owner pulse"))

    row = dashboard._hero_row(pulse)

    assert isinstance(row, ft.ResponsiveRow)
    assert row.rtl is True
    assert [control.col for control in row.controls] == [
        {"xs": 12, "md": 6},
        {"xs": 12, "md": 6},
    ]
    # RTL puts the first child on the right: currency first, pulse second.
    assert row.controls[0].content is not pulse
    assert row.controls[1].content is pulse
