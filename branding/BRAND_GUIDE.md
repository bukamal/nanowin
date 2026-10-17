# Nano | نانو — Brand Guide

## Brand Essence
Nano is a modern Arabic business application for accounting, inventory, invoices, and local-first operations.

## Core Logo
- Master artwork: `branding/source/brand.png` — the one square image the
  entire identity is derived from. Swap this file to rebrand the app.
- Primary logo: `logo_primary.png`.
- App icon: `app_icon.png` / `nano_app_icon.png`.
- In-app mark / badges: `src/assets/icon.png`.
- Splash screens: `src/assets/splash*.png` (light + dark variants).

Every one of those files is generated deterministically from the master by
`tools/generate_brand_assets.py` (`python3 tools/generate_brand_assets.py`),
so the mark cannot drift between Windows/Android/iOS/web/macOS again. To
rebrand, replace `branding/source/brand.png` and re-run the script — do not
hand-edit the individual PNGs.

## Brand Palette
The app theme (`src/nano_offline/core/theme.py`) is the single source of truth.
- Primary teal: `#0F766E` / deep teal: `#115E59`
- Violet: `#7C3AED` (gradient end) / accent aqua: `#2DD4BF`
- Surface: `#FFFFFF`
- Background: `#F8FAFC`
- Muted Text: `#64748B`

## Typography
- Latin: modern rounded sans-serif
- Arabic: clean geometric Arabic sans

## Packaging Notes
- Flet automatically picks icons and splash images from `src/assets/`.
- Android app icon background color is configured in `pyproject.toml`.
- APK build command: `uv run flet build apk` or use `build_nano_apk.sh`.
