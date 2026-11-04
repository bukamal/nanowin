"""Global UI text scaling.

The admin "font size" setting has always driven the printed documents, but
the on-screen UI ignored it: nearly every widget in the app passes an
explicit ``size=`` to ``ft.Text``, so a ``page.theme`` base size has nothing
to scale. Rather than touch hundreds of call sites, this module scales the
*creation* of the size-bearing controls once, at the single place they are
all built:

- ``ft.Text.size``
- ``ft.TextStyle.size`` (labels, field text, button text styles)
- ``ft.TextField.text_size``

``main._apply_theme`` sets the scale factor from the font-size setting and
calls :func:`install` once. Changing the setting rebuilds the shell (the
admin screen already calls ``on_theme_changed``), so freshly-built controls
pick up the new factor. Icons/padding are intentionally left alone.
"""

from __future__ import annotations

import flet as ft

_MIN_SCALE = 0.6
_MAX_SCALE = 2.0

_scale = 1.0
_installed = False


def get_scale() -> float:
    return _scale


def set_scale(factor) -> None:
    global _scale
    try:
        value = float(factor)
    except (TypeError, ValueError):
        value = 1.0
    _scale = max(_MIN_SCALE, min(_MAX_SCALE, value))


def scale_size(value):
    """Scale a font size for the current factor; non-numeric pass through."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    scaled = number * _scale
    rounded = round(scaled)
    return rounded if abs(scaled - rounded) < 0.05 else round(scaled, 1)


def install() -> None:
    """Wrap the size-bearing Flet constructors (idempotent)."""
    global _installed
    if _installed:
        return

    orig_text_init = ft.Text.__init__

    def text_init(self, *args, **kwargs):
        if kwargs.get("size") is not None:
            kwargs["size"] = scale_size(kwargs["size"])
        orig_text_init(self, *args, **kwargs)

    orig_style_init = ft.TextStyle.__init__

    def style_init(self, *args, **kwargs):
        if kwargs.get("size") is not None:
            kwargs["size"] = scale_size(kwargs["size"])
        orig_style_init(self, *args, **kwargs)

    orig_field_init = ft.TextField.__init__

    def field_init(self, *args, **kwargs):
        if kwargs.get("text_size") is not None:
            kwargs["text_size"] = scale_size(kwargs["text_size"])
        orig_field_init(self, *args, **kwargs)

    ft.Text.__init__ = text_init
    ft.TextStyle.__init__ = style_init
    ft.TextField.__init__ = field_init
    _installed = True


__all__ = ["get_scale", "set_scale", "scale_size", "install"]
