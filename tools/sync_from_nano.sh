#!/usr/bin/env bash
# Pull the shared tree from the ../nano checkout into this repo.
# One direction ONLY (nano -> nanowin): shared business logic is developed
# in `nano` and synced here. nanowin-owned files below are never touched.
set -euo pipefail

SRC="${1:-../nano}"
DST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$SRC/src/nano_offline" ]; then
  echo "error: $SRC does not look like the nano checkout" >&2
  exit 1
fi

rsync -a --delete \
  --exclude '/.git/' \
  --exclude '/.venv/' \
  --exclude '/build/' \
  --exclude '/dist/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '.coverage*' \
  --exclude '/src/nano_offline/core/platform_capabilities.py' \
  --exclude '/tools/sync_from_nano.sh' \
  --exclude '/.github/workflows/build-windows.yml' \
  --exclude '/.github/workflows/build-android-apk.yml' \
  --exclude '/windows/' \
  --exclude '/docs/WINDOWS_BUILD_AR.md' \
  --exclude '/NANOWIN.md' \
  "$SRC/" "$DST/"

echo "synced nano -> nanowin (nanowin-owned files preserved)"
