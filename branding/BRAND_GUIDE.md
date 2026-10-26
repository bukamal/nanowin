# Cashir | كاشير — Brand Guide

## Brand Essence
Cashir is a modern Arabic point-of-sale and sales-management application for
local-first shops: fast checkout, invoices, inventory, and offline operation.

## Core Logo
- Master artwork: `branding/source/brand.png` — the one square image the
  entire identity is derived from. Swap this file to rebrand the app.
- Primary logo: `logo_primary.png`.
- App icon: `app_icon.png` / `cashir_app_icon.png`.
- In-app mark / badges: `src/assets/icon.png`.
- Splash screens: `src/assets/splash*.png` (light + dark variants).

Every one of those files is generated deterministically from the master by
`tools/generate_brand_assets.py` (`python3 tools/generate_brand_assets.py`),
so the mark cannot drift between Windows/Android/iOS/web/macOS again. To
rebrand, replace `branding/source/brand.png` and re-run the script — do not
hand-edit the individual PNGs.

The current master is full-bleed dark artwork, so launcher icons stay opaque;
the in-app and Windows tiles get rounded corners. If a light "paper" master is
swapped in instead, the generator keys its pale background to transparency
automatically.

## Brand Palette
The app theme (`src/nano_offline/core/theme.py`) is the single source of truth.
- Primary blue: `#0B5FCC` / deep blue: `#0A4CAB`
- Brand cyan accent: `#0EA5E9` (gradient end)
- Soft blue surface: `#EAF3FF`
- Dark surface: `#0C264A`
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
- Internal package/org ids (`nano-offline`, `com.nano`) intentionally stay
  unchanged so existing installations keep updating in place without losing
  local data.
