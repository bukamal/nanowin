"""Design tokens: one source of truth for spacing, radius, motion and sizes.

Screens and the UI kit should read these instead of hardcoding pixel values,
so the whole app shares the same rhythm (4/8 grid), the same radii and the
same motion durations. Colors stay in ``core.theme`` (mode/brand aware); this
module only maps *function* to an existing color token via ``action_color``.
"""

from __future__ import annotations

from nano_offline.core.theme import Colors

__all__ = [
    "SPACING", "RADIUS", "MOTION", "SIZES", "ACTION_TOKENS",
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