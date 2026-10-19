"""Regenerate every Nano brand asset from one master image.

The whole visual identity -- launcher icons for Windows/Android/iOS/web/
macOS, the in-app mark, the branding lockup and the splash screens -- is
derived from a single square master kept in the repo
(``branding/source/brand.png``). Running this script repaints every
platform's asset from that master, so the mark can never drift between
platforms again, and swapping the branding is a one-file change (drop a
new master in and re-run) instead of hunting down a dozen PNGs. Launcher
icons get the master's flat paper background keyed to transparency (see
``TRANSPARENT_ICONS``) so the mark sits cleanly on taskbars and adaptive
icon backdrops; opaque targets (iOS/macOS tiles, splash artwork, in-app
pngs) keep the master untouched.

Only Pillow is required; there is no per-platform drawing code to keep in
sync anymore.

Usage::

    python3 tools/generate_brand_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "assets"
BRANDING = ROOT / "branding"
SOURCE = BRANDING / "source" / "brand.png"

ICON_SIZE = 1024
SPLASH_SIZE = 1242
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

# Launcher icons paint over OS-controlled surfaces (Windows taskbar,
# Android adaptive-icon background), so the master's flat paper background
# would show as a white sticker there. These are shipped with the paper
# keyed to transparency; everything that is expected to be opaque (iOS
# icons, macOS tiles, splash artwork, in-app branding pngs) keeps the
# master as-is.
TRANSPARENT_ICONS = ("icon", "icon_android", "icon_web", "icon_windows")


def _master() -> Image.Image:
    if not SOURCE.exists():
        raise SystemExit(f"missing brand master image: {SOURCE}")
    return Image.open(SOURCE).convert("RGBA")


def _transparent_icon(master: Image.Image) -> Image.Image:
    """Key the master's pale background to transparency.

    ``alpha = 255 - min(r, g, b)``: the mark's strokes are strongly
    coloured (min channel far from 255) while paper is near white, and the
    anti-aliased edges land on proportional alpha values, so the logo
    keeps smooth edges on any launcher backdrop.
    """
    img = master.copy()
    r, g, b, _a = img.split()
    paper = ImageChops.darker(ImageChops.darker(r, g), b)
    img.putalpha(paper.point(lambda v: 255 - v))
    return img


def _square(img: Image.Image, size: int) -> Image.Image:
    """Centre-crop to a square (only if the master isn't already one) and
    resize to ``size`` -- keeps a non-square master from ever distorting."""
    w, h = img.size
    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    return img


def _save_png(path: Path, img: Image.Image, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _square(img, size).save(path)


def main() -> None:
    master = _master()
    transparent = _transparent_icon(master)
    ASSETS.mkdir(parents=True, exist_ok=True)
    BRANDING.mkdir(parents=True, exist_ok=True)

    # In-app mark + OS launcher tiles + branding lockup. Launcher tiles get
    # the keyed background, the in-app/branding pngs stay opaque.
    for name in ("icon", "icon_android", "icon_ios", "icon_macos", "icon_web", "icon_windows"):
        _save_png(ASSETS / f"{name}.png", transparent if name in TRANSPARENT_ICONS else master, ICON_SIZE)
    for name in ("app_icon", "nano_app_icon", "logo_primary"):
        _save_png(BRANDING / f"{name}.png", master, ICON_SIZE)

    # Windows multi-resolution icon.
    _square(transparent, max(s for s, _ in ICO_SIZES)).save(
        ASSETS / "icon_windows.ico", sizes=ICO_SIZES
    )

    # Splash screens (light + dark share the same artwork).
    for name in (
        "splash", "splash_android", "splash_ios", "splash_web",
        "splash_dark", "splash_dark_android", "splash_dark_ios", "splash_dark_web",
    ):
        _save_png(ASSETS / f"{name}.png", master, SPLASH_SIZE)

    print(f"brand assets regenerated from {SOURCE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
