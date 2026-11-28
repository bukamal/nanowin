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
    # The UI font size (separate from the print size) scales the Material text
    # theme so controls that rely on the theme's default sizes follow it.
    scale = max(0.7, min(1.6, font_settings.ui_scale_factor(settings)))

    def _px(base: int) -> int:
        return max(8, round(base * scale))

    # Global bold: push every Material text style to W_700 so controls that
    # inherit their weight from the theme follow it (explicit per-widget
    # weights still win, which is desirable).
    _bold = {"weight": ft.FontWeight.W_700} if font_settings.ui_bold(settings) else {}
    text_theme = ft.TextTheme(
        body_large=ft.TextStyle(size=_px(16), color=theme.Colors.TEXT_PRIMARY, **_bold),
        body_medium=ft.TextStyle(size=_px(14), color=theme.Colors.TEXT_PRIMARY, **_bold),
        body_small=ft.TextStyle(size=_px(12), color=theme.Colors.TEXT_PRIMARY, **_bold),
        label_large=ft.TextStyle(size=_px(14), color=theme.Colors.TEXT_PRIMARY, **_bold),
        label_medium=ft.TextStyle(size=_px(12), color=theme.Colors.TEXT_PRIMARY, **_bold),
        label_small=ft.TextStyle(size=_px(11), color=theme.Colors.TEXT_PRIMARY, **_bold),
        title_large=ft.TextStyle(size=_px(22), color=theme.Colors.TEXT_PRIMARY, **_bold),
        title_medium=ft.TextStyle(size=_px(16), color=theme.Colors.TEXT_PRIMARY, **_bold),
        title_small=ft.TextStyle(size=_px(14), color=theme.Colors.TEXT_PRIMARY, **_bold),
    )
    page_theme = ft.Theme(
        color_scheme_seed=theme.Colors.PRIMARY,
        font_family=fam,
        text_theme=text_theme,
        visual_density=appearance_settings.visual_density(settings),
        # Shape setting applied app-wide through the Material theme's shapes, so
        # "soft/standard/sharp" reflects on every card, dialog, sheet, menu,
        # chip and button -- not just the handful of components that already
        # called radius_for() by hand.
        card_theme=ft.CardTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("md"))),
        dialog_theme=ft.DialogTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("lg"))),
        bottom_sheet_theme=ft.BottomSheetTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("lg"))),
        list_tile_theme=ft.ListTileTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("md"))),
        snackbar_theme=ft.SnackBarTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("sm"))),
        filled_button_theme=ft.FilledButtonTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("lg"))),
        outlined_button_theme=ft.OutlinedButtonTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("lg"))),
        elevated_button_theme=ft.ElevatedButtonTheme(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("lg"))),
        segmented_button_theme=ft.SegmentedButtonTheme(style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=tokens.radius_for("lg")))),
    )
    return page_theme, (ft.ThemeMode.DARK if dark else ft.ThemeMode.LIGHT)