from __future__ import annotations

import flet as ft

from nano_offline.views.admin_view import AdminCenter


def test_admin_settings_use_responsive_two_column_layout():
    description = ft.Text("description")
    toggle = ft.Switch(label="setting")
    field = ft.TextField(label="field")
    divider = ft.Divider()
    options = ft.ResponsiveRow()
    controls = [description, toggle, field, divider, options]

    layout = AdminCenter._settings_columns(controls)

    assert isinstance(layout, ft.ResponsiveRow)
    assert layout.rtl is True
    assert [item.col for item in layout.controls] == [
        {"xs": 12, "md": 12},
        {"xs": 12, "md": 6},
        {"xs": 12, "md": 6},
        {"xs": 12, "md": 12},
        {"xs": 12, "md": 12},
    ]
    assert [item.content for item in layout.controls] == controls
