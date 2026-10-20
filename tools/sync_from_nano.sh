#!/usr/bin/env bash
# Pull the shared tree from the ../nano checkout into this repo.
# One direction ONLY (nano -> nanowin): shared business logic is developed
# in `nano` and synced here. nanowin-owned files below are never touched.
set -euo pipefail

DST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="../nano"
DRY_RUN=1
ALLOW_DELETE=0
APPLY=0
SOURCE_SET=0

usage() {
  cat <<'EOF'
Usage: tools/sync_from_nano.sh [source] [--dry-run | --apply] [--delete]

Default: preview the nano -> nanowin sync without changing files.
--apply: apply additions and updates, but leave destination-only files alone.
--delete: include destination-only files in the operation (preview by default).
          Applying deletions requires both --delete and --apply.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      APPLY=0
      ;;
    --apply)
      APPLY=1
      DRY_RUN=0
      ;;
    --delete)
      ALLOW_DELETE=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if ((SOURCE_SET)); then
        echo "error: only one source directory may be specified" >&2
        usage >&2
        exit 2
      fi
      SRC="$1"
      SOURCE_SET=1
      ;;
  esac
  shift
done

if ! command -v rsync >/dev/null 2>&1; then
  echo "error: rsync is required" >&2
  exit 1
fi

if [ ! -d "$SRC/src/nano_offline" ]; then
  echo "error: $SRC does not look like the nano checkout" >&2
  exit 1
fi
SRC="$(cd "$SRC" && pwd)"

if [[ "$SRC" == "$DST" || "$SRC" == "/" || "$DST" == "/" ]]; then
  echo "error: refusing to synchronize identical or root directories" >&2
  exit 1
fi
case "$DST/" in
  "$SRC/"*)
    echo "error: destination is inside source; refusing recursive synchronization" >&2
    exit 1
    ;;
esac
case "$SRC/" in
  "$DST/"*)
    echo "error: source is inside destination; refusing recursive synchronization" >&2
    exit 1
    ;;
esac

RSYNC_ARGS=(-a --itemize-changes)
if ((DRY_RUN)); then
  RSYNC_ARGS+=(--dry-run)
fi
if ((ALLOW_DELETE)); then
  RSYNC_ARGS+=(--delete)
fi

echo "Source:      $SRC"
echo "Destination: $DST"
if ((DRY_RUN)); then
  echo "Mode:        preview only (no files will change)"
elif ((ALLOW_DELETE)); then
  echo "Mode:        apply changes and delete destination-only files"
else
  echo "Mode:        apply additions and updates; preserve destination-only files"
fi

rsync "${RSYNC_ARGS[@]}" \
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

if ((DRY_RUN)); then
  echo "preview complete; rerun with --apply to apply updates"
elif ((ALLOW_DELETE)); then
  echo "synced nano -> nanowin, including explicitly authorized deletions"
else
  echo "synced nano -> nanowin; destination-only files were preserved"
fi
