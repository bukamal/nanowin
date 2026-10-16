"""Regenerate Nano's brand assets (app icon + splash) deterministically.

The old icon shipped as a heavy, low-contrast raster. This script draws a
single crisp geometric mark -- a bold, rounded monogram "N" -- and derives
every platform asset from it, so the icon can never drift between Windows,
Android, iOS, web, macOS and the in-app brand badges again.

Design contract:

* ``icon.png``            transparent, full-colour mark  (used *inside* the
                          app: white login/activation/command-bar badges).
* ``icon_<platform>.png`` full gradient tile + white mark (OS launchers, so
                          the icon reads on any wallpaper).
* ``icon_windows.ico``    same tile, multi-resolution.
* ``splash*.png``         transparent canvas with a centred white mark, so
                          it sits on the teal splash colour configured in
                          pyproject.toml.

Run: ``python3 tools/generate_brand_assets.py``
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "assets"
BRANDING = ROOT / "branding"

# Brand palette (kept in lock-step with core/theme.py + BRAND_GUIDE.md).
TEAL = (15, 118, 110)      # PRIMARY  #0F766E
TEAL_DARK = (17, 94, 89)   # PRIMARY_DARK #115E59
AQUA = (45, 212, 191)      # #2DD4BF
VIOLET = (124, 58, 237)    # PURPLE #7C3AED
WHITE = (255, 255, 255)

SS = 2048  # supersample canvas; every asset is downscaled from this.


def _diagonal_gradient(size: int, c0: tuple, c1: tuple) -> Image.Image:
    """Smooth top-left -> bottom-right gradient between two RGB colours."""
    ramp = np.linspace(0.0, 1.0, size, dtype=np.float32)
    t = (ramp[None, :] + ramp[:, None]) / 2.0
    t = t[..., None]
    a = np.array(c0, dtype=np.float32)
    b = np.array(c1, dtype=np.float32)
    rgb = (a * (1.0 - t) + b * t).astype(np.uint8)
    return Image.fromarray(rgb, "RGB")


def _n_mark(size: int, stroke: int, *, inset: float = 0.30) -> Image.Image:
    """Draw the monogram "N" as an anti-aliased alpha mask."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    span = size * (1.0 - 2 * inset)
    x0 = size * inset
    x1 = size - size * inset
    yt = size * inset
    yb = size - size * inset
    half = stroke / 2
    # Left stem, right stem, then the diagonal (round joints hide the seams).
    for (ax, ay, bx, by) in [
        (x0, yt + half, x0, yb - half),
        (x1, yt + half, x1, yb - half),
        (x0 + half, yt + half, x1 - half, yb - half),
    ]:
        draw.line([(ax, ay), (bx, by)], fill=255, width=stroke, joint="curve")
        for (cx, cy) in [(ax, ay), (bx, by)]:
            draw.ellipse([cx - half, cy - half, cx + half, cy + half], fill=255)
    del span
    return mask


def _rounded_tile(size: int, grad: Image.Image) -> Image.Image:
    """Full-bleed gradient square with soft rounded corners."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.225), fill=255
    )
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(grad.convert("RGBA"), (0, 0), mask)
    return tile


def _mark_rgba(size: int, *, white: bool) -> Image.Image:
    """The mark on a transparent canvas -- full-colour or pure white."""
    mask = _n_mark(size, stroke=int(size * 0.145))
    rgba = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if white:
        rgba.paste(WHITE, (0, 0), mask)
    else:
        rgba.paste(_diagonal_gradient(size, TEAL, VIOLET).convert("RGBA"), (0, 0), mask)
    return rgba


def _save_icon_tile(path: Path, size: int) -> None:
    grad = _diagonal_gradient(SS, TEAL, VIOLET)
    tile = _rounded_tile(SS, grad)
    tile.alpha_composite(_mark_rgba(SS, white=True))
    tile.resize((size, size), Image.LANCZOS).save(path)


def _save_mark(path: Path, size: int, *, white: bool) -> None:
    _mark_rgba(SS, white=white).resize((size, size), Image.LANCZOS).save(path)


def _save_splash(path: Path, *, dark: bool) -> None:
    # Transparent canvas; the mark is placed on the splash colour Flet paints
    # underneath (teal / deep teal), so it stays legible in both themes.
    canvas = Image.new("RGBA", (1242, 1242), (0, 0, 0, 0))
    mark = _mark_rgba(SS, white=True).resize((430, 430), Image.LANCZOS)
    canvas.alpha_composite(mark, ((1242 - 430) // 2, (1242 - 430) // 2))
    canvas.save(path)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    BRANDING.mkdir(parents=True, exist_ok=True)

    # In-app mark (transparent) + full-colour logo lockup.
    _save_mark(ASSETS / "icon.png", 1024, white=False)
    _save_mark(BRANDING / "logo_primary.png", 1024, white=False)

    # OS launcher tiles.
    for name in ("icon_android", "icon_ios", "icon_web", "icon_macos", "icon_windows"):
        _save_icon_tile(ASSETS / f"{name}.png", 1024)
    _save_icon_tile(BRANDING / "app_icon.png", 1024)
    _save_icon_tile(BRANDING / "nano_app_icon.png", 1024)

    # Windows .ico (multi-resolution).
    grad = _diagonal_gradient(SS, TEAL, VIOLET)
    tile = _rounded_tile(SS, grad)
    tile.alpha_composite(_mark_rgba(SS, white=True))
    tile.resize((256, 256), Image.LANCZOS).save(
        ASSETS / "icon_windows.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # Splash (light + dark share the same white mark on transparent).
    for name in ("splash", "splash_android", "splash_ios", "splash_web"):
        _save_splash(ASSETS / f"{name}.png", dark=False)
    for name in ("splash_dark", "splash_dark_android", "splash_dark_ios", "splash_dark_web"):
        _save_splash(ASSETS / f"{name}.png", dark=True)

    print("brand assets regenerated")


if __name__ == "__main__":
    main()
