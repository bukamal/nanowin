"""Global UI text scaling helpers."""

from __future__ import annotations

import flet as ft

from nano_offline.core import font_settings, ui_scale


class _Settings:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key, default=None):
        return self._data.get(key, default)


def test_scale_size_math():
    ui_scale.set_scale(1.5)
    assert ui_scale.scale_size(10) == 15
    assert ui_scale.scale_size(13) == 19.5
    assert ui_scale.scale_size(None) is None
    assert ui_scale.scale_size("auto") == "auto"
    ui_scale.set_scale(1.0)  # restore for the rest of the process


def test_scale_is_clamped():
    ui_scale.set_scale(0.01)
    assert ui_scale.get_scale() >= 0.6
    ui_scale.set_scale(99)
    assert ui_scale.get_scale() <= 2.0
    ui_scale.set_scale(1.0)


def test_install_at_unit_scale_is_identity_and_idempotent():
    ui_scale.set_scale(1.0)
    ui_scale.install()
    ui_scale.install()  # second call must not double-wrap
    assert ft.Text("x", size=14).size == 14
    assert ft.Text("x").size is None


def test_font_size_factor():
    assert font_settings.ui_scale_factor(_Settings()) == 1.0
    assert font_settings.ui_scale_factor(
        _Settings({font_settings.SETTING_SIZE: "17"})
    ) == 17 / font_settings.DEFAULT_SIZE