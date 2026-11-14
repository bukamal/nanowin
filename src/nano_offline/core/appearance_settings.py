"""Admin-configurable *shape* preferences (beyond light/dark + font).

Device/session choices stored in the open-ended settings table (same pattern
as ``theme_settings``/``font_settings``): shape, density, motion and contrast.
Kept free of a flet import except for the density mapping helper.
"""

from __future__ import annotations

SETTING_SHAPE = "ui.shape"          # standard | soft | sharp
SETTING_DENSITY = "ui.density"      # comfortable | compact | touch
SETTING_MOTION = "ui.motion"        # 1/0
SETTING_CONTRAST = "ui.contrast"    # normal | high

DEFAULT_SHAPE = "standard"
DEFAULT_DENSITY = "comfortable"
DEFAULT_MOTION = True
DEFAULT_CONTRAST = "normal"

SHAPES = [("standard", "قياسي"), ("soft", "ناعم"), ("sharp", "حاد")]
DENSITIES = [("comfortable", "مريح"), ("compact", "مضغوط"), ("touch", "لمس كبير")]
CONTRASTS = [("normal", "عادي"), ("high", "تباين عالٍ")]


def _get(settings, key: str, default: str) -> str:
    try:
        value = settings.get(key, default)
    except Exception:
        value = default
    return str(value or "").strip().lower() or default


def shape(settings) -> str:
    value = _get(settings, SETTING_SHAPE, DEFAULT_SHAPE)
    return value if value in dict(SHAPES) else DEFAULT_SHAPE


def density(settings) -> str:
    value = _get(settings, SETTING_DENSITY, DEFAULT_DENSITY)
    return value if value in dict(DENSITIES) else DEFAULT_DENSITY


def motion_enabled(settings) -> bool:
    value = _get(settings, SETTING_MOTION, "1" if DEFAULT_MOTION else "0")
    return value in {"1", "true", "yes", "on"}


def contrast(settings) -> str:
    value = _get(settings, SETTING_CONTRAST, DEFAULT_CONTRAST)
    return value if value in dict(CONTRASTS) else DEFAULT_CONTRAST


def set_shape(settings, value: str) -> None:
    settings.set(SETTING_SHAPE, value if value in dict(SHAPES) else DEFAULT_SHAPE)


def set_density(settings, value: str) -> None:
    settings.set(SETTING_DENSITY, value if value in dict(DENSITIES) else DEFAULT_DENSITY)


def set_motion(settings, value: bool) -> None:
    settings.set(SETTING_MOTION, "1" if value else "0")


def set_contrast(settings, value: str) -> None:
    settings.set(SETTING_CONTRAST, value if value in dict(CONTRASTS) else DEFAULT_CONTRAST)


def visual_density(settings):
    """Map the density choice to a flet VisualDensity (or None = default)."""
    try:
        import flet as ft
    except Exception:
        return None
    if density(settings) == "compact":
        return ft.VisualDensity.COMPACT
    if density(settings) == "touch":
        return ft.VisualDensity.STANDARD
    return ft.VisualDensity.STANDARD