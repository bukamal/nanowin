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


def test_embedded_notification_section_avoids_nested_responsive_wrap():
    controls = [ft.Text("description"), ft.Column([ft.Text("embedded settings")])]
    admin = AdminCenter.__new__(AdminCenter)

    section = admin._section("الإشعارات والتنبيهات", controls, responsive=False)
    body = section.content.controls[2]

    assert isinstance(body, ft.Column)
    assert body.controls == controls
