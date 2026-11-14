"""Build the app's flet Theme/DarkTheme from the configured design settings.

One place turns (accent color + font family + density + shape) into the
``page.theme`` / ``theme_mode`` the whole app renders with, so every screen
shares exactly one visual definition.
"""

from __future__ import annotations

import flet as ft

from nano_offline.core import appearance_settings, font_settings, theme
from nano_offline.core.design import tokens

__all__ = ["build_page_theme"]


def build_page_theme(settings, *, family: str | None = None, is_dark: bool | None = None):
    """Return ``(ft.Theme, ft.ThemeMode)`` for the current settings."""
    fam = family if family is not None else font_settings.ui_family(settings, "auto")
    dark = theme.is_dark() if is_dark is None else bool(is_dark)
    tokens.set_shape(appearance_settings.shape(settings))
    page_theme = ft.Theme(
        color_scheme_seed=theme.Colors.PRIMARY,
        font_family=fam,
        visual_density=appearance_settings.visual_density(settings),
    )
    return page_theme, (ft.ThemeMode.DARK if dark else ft.ThemeMode.LIGHT)