"""Design tokens: one source of truth for spacing, radius, motion and sizes.

Screens and the UI kit should read these instead of hardcoding pixel values,
so the whole app shares the same rhythm (4/8 grid), the same radii and the
same motion durations. Colors stay in ``core.theme`` (mode/brand aware); this
module only maps *function* to an existing color token via ``action_color``.
"""

from __future__ import annotations

from nano_offline.core.theme import Colors

__all__ = [
    "SPACING", "RADIUS", "MOTION", "SIZES", "ACTION_TOKENS", "POS_DENSITY",
    "action_color", "radius_for", "set_shape",
]

# Current shape preference (soft/standard/sharp), applied to radius tokens
# that don't pass an explicit shape. Set once when the theme is applied.
_state = {"shape": "standard"}


def set_shape(shape: str) -> None:
    _state["shape"] = str(shape or "standard")

# 4/8 spacing scale.
SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}

# Radius scale; `shape` (from appearance settings) shifts the base.
RADIUS = {"sm": 8, "md": 12, "lg": 16, "xl": 20, "pill": 999}

# Motion durations (ms) -- ease-out is applied by callers.
MOTION = {"fast": 120, "base": 160, "slow": 220}

# Common control sizes (touch targets >= 48).
SIZES = {
    "button_sm": 36,
    "button_md": 44,
    "button_lg": 56,
    "cta": 60,
    "touch_target": 48,
    "tile_w": 170,
    "tile_h": 150,
    "rail_w": 270,
    "cart_w": 400,
    "cart_row": 64,
}

# POS layout sizes by density: bigger targets for large touch screens.
# ``tile_h`` is a *fixed* height (the grid previously left desktop tiles
# unsized, so rows were ragged): room for the icon, a two-line name block,
# the price and the corner stock badge with a little breathing space.
# Every item-card metric (icon glyph, icon box, name/price text, padding,
# accent strip) lives here too, so the POS grid -- and restaurant mode, which
# reuses the same tile -- is driven entirely by the one "density" appearance
# setting instead of hard-coded numbers scattered in the view.
POS_DENSITY = {
    "compact": {
        "rail": 180, "cart": 340, "tile_h": 120, "row": 46,
        "tile_icon": 20, "tile_icon_box": 36, "tile_name": 11, "tile_price": 12,
        "tile_pad": 8, "tile_strip": 4, "tile_spacing": 4,
    },
    "comfortable": {
        "rail": 210, "cart": 420, "tile_h": 136, "row": 54,
        "tile_icon": 26, "tile_icon_box": 44, "tile_name": 12, "tile_price": 13,
        "tile_pad": 10, "tile_strip": 4, "tile_spacing": 5,
    },
    "touch": {
        "rail": 290, "cart": 470, "tile_h": 172, "row": 66,
        "tile_icon": 34, "tile_icon_box": 58, "tile_name": 15, "tile_price": 16,
        "tile_pad": 14, "tile_strip": 5, "tile_spacing": 6,
    },
}


def pos_sizes(density: str) -> dict:
    return POS_DENSITY.get(str(density or "comfortable"), POS_DENSITY["comfortable"])

# Function -> theme color token (mode aware). Use these for button variants so
# colors are semantic, not arbitrary.
ACTION_TOKENS = {
    "primary": "PRIMARY",
    "success": "SUCCESS",
    "danger": "DANGER",
    "warning": "WARNING_DARKER",
    "info": "PRIMARY_DARK",
    "neutral": "TEXT_MUTED",
}


def action_color(action: str) -> str:
    return getattr(Colors, ACTION_TOKENS.get(str(action or "primary"), "PRIMARY"))


def radius_for(name: str = "md", shape: str | None = None) -> int:
    """Radius token adjusted by the configured shape (soft/standard/sharp)."""
    base = RADIUS.get(name, RADIUS["md"])
    if base >= RADIUS["pill"]:
        return base
    shape = shape if shape is not None else _state.get("shape", "standard")
    delta = {"soft": 4, "sharp": -4}.get(str(shape or "standard"), 0)
    return max(2, base + delta)