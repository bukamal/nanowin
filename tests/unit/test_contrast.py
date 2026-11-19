"""Contrast guard for the core text/surface token pairs (light + dark).

A dark-mode mismatch (light text on a literal white surface, or theme text
falling back to black) is exactly what made names/amounts disappear. This
fails if any primary/secondary text token drops below WCAG AA 4.5:1 against
the surfaces it is used on, in either mode.
"""

from __future__ import annotations

import pytest

from nano_offline.core import theme


def _luminance(hex_color: str) -> float:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _ratio(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [
    # (foreground token, background token) used for real body text.
    ("TEXT_PRIMARY", "WHITE"),
    ("TEXT_PRIMARY", "BACKGROUND"),
    ("TEXT_PRIMARY", "BACKGROUND_ALT"),
    ("TEXT_PRIMARY", "PRIMARY_BG"),
    ("TEXT_SECONDARY", "WHITE"),
    ("TEXT_SECONDARY", "BACKGROUND"),
    ("TEXT_SECONDARY", "BACKGROUND_ALT"),
    ("TEXT_MUTED", "WHITE"),
    ("TEXT_MUTED", "BACKGROUND_ALT"),
    ("PRIMARY_DARK", "WHITE"),
    ("PRIMARY_DARK", "PRIMARY_BG"),
    # On-accent text (buttons / tinted status surfaces).
    ("WHITE", "PRIMARY"),
    ("TEXT_PRIMARY", "SUCCESS_BG"),
    ("TEXT_PRIMARY", "DANGER_BG"),
    ("TEXT_PRIMARY", "WARNING_BG"),
    ("TEXT_PRIMARY", "PURPLE_BG"),
    ("SUCCESS_DARKER", "SUCCESS_BG"),
    ("DANGER_DARKER", "DANGER_BG"),
    ("WARNING_DARKER", "WARNING_BG"),
]


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_text_token_contrast_meets_aa(mode, fg, bg):
    previous = theme.get_mode()
    theme.set_mode(mode)
    try:
        foreground = getattr(theme.Colors, fg)
        background = getattr(theme.Colors, bg)
        ratio = _ratio(foreground, background)
        assert ratio >= 4.5, f"{mode}: {fg} on {bg} = {ratio:.2f}:1 (<4.5)"
    finally:
        theme.set_mode(previous)