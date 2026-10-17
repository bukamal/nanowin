"""Regenerate every Nano brand asset from one master image.

The whole visual identity -- launcher icons for Windows/Android/iOS/web/
macOS, the in-app mark, the branding lockup and the splash screens -- is
derived from a single square master kept in the repo
(``branding/source/brand.png``). Running this script repaints every
platform's asset from that master, so the mark can never drift between
platforms again, and swapping the branding is a one-file change (drop a
new master in and re-run) instead of hunting down a dozen PNGs.

Only Pillow is required; there is no per-platform drawing code to keep in
sync anymore.

Usage::

    python3 tools/generate_brand_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "assets"
BRANDING = ROOT / "branding"
SOURCE = BRANDING / "source" / "brand.png"

ICON_SIZE = 1024
SPLASH_SIZE = 1242
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _master() -> Image.Image:
    if not SOURCE.exists():
        raise SystemExit(f"missing brand master image: {SOURCE}")
    return Image.open(SOURCE).convert("RGBA")


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
    ASSETS.mkdir(parents=True, exist_ok=True)
    BRANDING.mkdir(parents=True, exist_ok=True)

    # In-app mark + OS launcher tiles + branding lockup.
    for name in ("icon", "icon_android", "icon_ios", "icon_macos", "icon_web", "icon_windows"):
        _save_png(ASSETS / f"{name}.png", master, ICON_SIZE)
    for name in ("app_icon", "nano_app_icon", "logo_primary"):
        _save_png(BRANDING / f"{name}.png", master, ICON_SIZE)

    # Windows multi-resolution icon.
    _square(master, max(s for s, _ in ICO_SIZES)).save(
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
