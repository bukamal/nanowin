"""Design system tests: tokens, appearance settings, theme builder, UI kit,
and a guard that the kit itself uses tokens (no raw hex)."""

from __future__ import annotations

import re
from pathlib import Path

import flet as ft

from nano_offline.core import appearance_settings, theme
from nano_offline.core.design import theme_builder, tokens
from nano_offline.components.ui import (
    badge,
    card,
    cta_button,
    filled_action_button,
    skeleton,
    sheet_footer,
)
from nano_offline.repositories.settings_repository import SettingsRepository
from nano_offline.core.database import Database


def _settings(tmp_path):
    db = Database(tmp_path / "ui.db")
    db.initialize()
    return SettingsRepository(db)


def test_appearance_defaults_and_setters(tmp_path):
    s = _settings(tmp_path)
    assert appearance_settings.shape(s) == "standard"
    assert appearance_settings.density(s) == "comfortable"
    assert appearance_settings.motion_enabled(s) is True
    assert appearance_settings.contrast(s) == "normal"

    appearance_settings.set_shape(s, "soft")
    appearance_settings.set_density(s, "touch")
    appearance_settings.set_motion(s, False)
    appearance_settings.set_contrast(s, "high")
    assert appearance_settings.shape(s) == "soft"
    assert appearance_settings.density(s) == "touch"
    assert appearance_settings.motion_enabled(s) is False
    assert appearance_settings.contrast(s) == "high"


def test_radius_shape_shifts(tmp_path):
    tokens.set_shape("standard")
    base = tokens.radius_for("lg")
    tokens.set_shape("soft")
    assert tokens.radius_for("lg") == base + 4
    tokens.set_shape("sharp")
    assert tokens.radius_for("lg") == base - 4
    tokens.set_shape("standard")


def test_action_colors_map_to_tokens():
    assert tokens.action_color("success") == theme.Colors.SUCCESS
    assert tokens.action_color("danger") == theme.Colors.DANGER
    assert tokens.action_color("unknown") == theme.Colors.PRIMARY


def test_theme_builder_returns_theme_and_mode(tmp_path):
    s = _settings(tmp_path)
    page_theme, mode = theme_builder.build_page_theme(s, family="Tahoma", is_dark=True)
    assert isinstance(page_theme, ft.Theme)
    assert mode == ft.ThemeMode.DARK


def test_ui_kit_builds_controls(tmp_path):
    assert isinstance(cta_button("دفع", on_click=lambda _=None: None), ft.FilledButton)
    assert isinstance(filled_action_button("تم", action="success"), ft.FilledButton)
    assert isinstance(sheet_footer(primary_label="حفظ", on_primary=None), ft.Row)
    assert isinstance(card(ft.Text("x")), ft.Container)
    assert isinstance(badge("جاهز", action="success"), ft.Container)
    assert isinstance(skeleton(), ft.Container)


def test_ui_kit_has_no_raw_hex_colours():
    """The kit must use theme tokens, not hardcoded hex colours."""
    root = Path(__file__).resolve().parents[2] / "src" / "nano_offline" / "components" / "ui"
    offenders = []
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"#[0-9a-fA-F]{6}\b", text):
            offenders.append(path.name)
    assert not offenders, f"raw hex colours found in UI kit: {offenders}"

def test_pos_density_sizes_scale():
    from nano_offline.core.design.tokens import pos_sizes

    touch = pos_sizes("touch")
    comfortable = pos_sizes("comfortable")
    compact = pos_sizes("compact")
    assert touch["rail"] > comfortable["rail"] > compact["rail"]
    assert touch["cart"] > comfortable["cart"] > compact["cart"]
    assert touch["tile_h"] > compact["tile_h"]
    assert pos_sizes("unknown") == comfortable
