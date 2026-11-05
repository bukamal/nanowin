from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Smoke scripts import nano_offline directly; guarantee src is importable
# even when the package isn't pip-installed (fresh CI runner).
env = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}


def _has_module(name: str) -> bool:
    return subprocess.run([sys.executable, "-c", f"import {name}"], cwd=ROOT, capture_output=True).returncode == 0


# One curated list, shared with the pytest wrapper (tests/contract) so the two
# runners can never drift apart.
sys.path.insert(0, str(ROOT / "tools"))
from smoke_scripts import SMOKE_SCRIPTS  # noqa: E402


print("▶ compileall", flush=True)
subprocess.run([sys.executable, "-m", "compileall", "-q", "src", "tools", "tests", "extensions/flet_native_files/src"], check=True, cwd=ROOT)

# ---------------------------------------------------------------------------
# pytest suite: tests/unit (pure-python) + tests/integration (real DB).
# Skips gracefully when pytest isn't installed so the gate is never *weaker*
# than it used to be -- the standalone smoke scripts below always run.
# ---------------------------------------------------------------------------
if _has_module("pytest"):
    print("▶ pytest", flush=True)
    # Explicit paths: unit + integration, and the wrapper that runs the curated
    # smoke/contract scripts (tests/contract/test_smoke_scripts_wrapped.py) so
    # they get per-script reporting instead of a second hand-rolled loop.
    pytest_targets = ["tests/unit", "tests/integration", "tests/contract/test_smoke_scripts_wrapped.py"]
    args = [sys.executable, "-m", "pytest", "-q", *pytest_targets]
    if _has_module("coverage"):
        args = [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q", *pytest_targets]
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    if _has_module("coverage"):
        print("▶ coverage report", flush=True)
        subprocess.run([sys.executable, "-m", "coverage", "report"], check=True, cwd=ROOT)
else:
    print("▶ pytest (skipped: pytest not installed in this environment)", flush=True)

# ---------------------------------------------------------------------------
# Standalone fallback: only when pytest is unavailable. The same curated list
# (tools/smoke_scripts.py) is what the pytest wrapper runs, so behaviour is
# identical either way; this just keeps the gate working without pytest.
# ---------------------------------------------------------------------------
if not _has_module("pytest"):
    print("▶ smoke/contract scripts (standalone; pytest unavailable)", flush=True)
    for script in SMOKE_SCRIPTS:
        print("▶", script, flush=True)
        subprocess.run([sys.executable, str(ROOT / script)], check=True, cwd=ROOT, env=env)
else:
    print("▶ smoke/contract scripts (via pytest wrapper)", flush=True)
print("quality_gate passed")
