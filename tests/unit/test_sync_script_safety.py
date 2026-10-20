from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "sync_from_nano.sh"


def _run_sync(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "nano"
    (source / "src" / "nano_offline").mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "rsync-args.txt"
    fake_rsync = fake_bin / "rsync"
    fake_rsync.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RSYNC_ARGS_LOG\"\n",
        encoding="utf-8",
    )
    fake_rsync.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        "RSYNC_ARGS_LOG": str(log),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), str(source), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    result.rsync_args = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result


def test_sync_defaults_to_non_destructive_preview(tmp_path):
    result = _run_sync(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.rsync_args
    assert "--delete" not in result.rsync_args


def test_sync_apply_preserves_destination_only_files(tmp_path):
    result = _run_sync(tmp_path, "--apply")

    assert result.returncode == 0, result.stderr
    assert "--dry-run" not in result.rsync_args
    assert "--delete" not in result.rsync_args


def test_deletions_require_apply_and_are_previewed_first(tmp_path):
    preview = _run_sync(tmp_path, "--delete")
    apply = _run_sync(tmp_path, "--delete", "--apply")

    assert preview.returncode == 0, preview.stderr
    assert "--dry-run" in preview.rsync_args
    assert "--delete" in preview.rsync_args
    assert apply.returncode == 0, apply.stderr
    assert "--dry-run" not in apply.rsync_args
    assert "--delete" in apply.rsync_args
