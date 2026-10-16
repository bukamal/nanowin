# Nano | نانو — Brand Guide

## Brand Essence
Nano is a modern Arabic business application for accounting, inventory, invoices, and local-first operations.

## Core Logo
- Mark: a bold rounded monogram **N**.
- Primary logo: `logo_primary.png` (transparent full-colour mark, for light surfaces).
- App icon: `app_icon.png` / `nano_app_icon.png` (gradient tile + white mark).
- In-app badges use `src/assets/icon.png` (transparent mark).

All assets are generated deterministically by `tools/generate_brand_assets.py`
(`python3 tools/generate_brand_assets.py`); the script is the source of truth,
so the icon cannot drift between Windows/Android/iOS/web/macOS again.

## Brand Palette
The app theme (`src/nano_offline/core/theme.py`) is the single source of truth.
- Primary teal: `#0F766E` / deep teal: `#115E59`
- Violet: `#7C3AED` (gradient end) / accent aqua: `#2DD4BF`
- Launcher icon: smooth teal -> violet diagonal gradient with a white N.
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
