import flet as ft

from nano_offline.core import font_settings
from nano_offline.core.design.theme_builder import build_page_theme


def test_ui_bold_setting_default_and_toggle():
    assert font_settings.ui_bold({}) is False
    assert font_settings.ui_bold({font_settings.SETTING_BOLD: "1"}) is True
    assert font_settings.ui_bold({font_settings.SETTING_BOLD: "0"}) is False


def test_bold_setting_weights_the_text_theme():
    theme, _mode = build_page_theme({
        font_settings.SETTING_FAMILY: "Arial",
        font_settings.SETTING_SIZE: "13",
        font_settings.SETTING_BOLD: "1",
    })
    assert theme.text_theme.body_medium.weight == ft.FontWeight.W_700
    assert theme.text_theme.title_large.weight == ft.FontWeight.W_700
