import flet as ft

from nano_offline.components import GridColumn
from nano_offline.components.detail_sheet import (
    detail_meta,
    detail_section,
    detail_sheet_content,
    detail_table,
)


def test_detail_table_builds_bounded_grid():
    table = detail_table([GridColumn("a", "A", width=80)], [{"a": "x"}], height=120)
    assert isinstance(table, ft.Container)
    assert table.height == 120


def test_detail_sheet_content_assembles_fixed_frame():
    content = detail_sheet_content(
        title="مادة",
        subtitle="مخزون",
        icon=ft.Icons.INVENTORY_2_OUTLINED,
        on_close=lambda _=None: None,
        meta=[detail_meta(ft.Icons.CATEGORY_OUTLINED, "التصنيف", "عام")],
        summary=ft.Text("ملخّص"),
        sections=[detail_section("حركات", detail_table([GridColumn("d", "التاريخ", width=100)], [{"d": "—"}], height=100))],
        actions=[ft.TextButton("إغلاق")],
    )
    assert isinstance(content, ft.Container)
    assert content.content is not None

def test_desktop_sheet_paints_white_surface_with_top_radius():
    from nano_offline.components import sheets
    from nano_offline.core.theme import Colors

    class _Page:
        width = 1280
        height = 800

    sheet, width = sheets.new_desktop_sheet(_Page())
    assert sheet.bgcolor == Colors.WHITE
    assert sheet.shape is not None
    assert width > 0


def test_detail_sheet_footer_strips_expand_from_wrapped_actions():
    expand_btn = ft.FilledButton("إغلاق", expand=True)
    content = detail_sheet_content(
        title="t", icon=ft.Icons.INFO, on_close=lambda _=None: None,
        sections=[ft.Text("body")], actions=[expand_btn],
    )
    # The action was created with expand=True; the template must clear it so a
    # wrapping Row never hosts an Expanded child (which renders a grey box).
    assert expand_btn.expand is False
    assert isinstance(content, ft.Container)
