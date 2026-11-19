"""Contract: no hard-coded hex surface colours in components/views.

A literal ``bgcolor="#FFFFFF"`` does not follow dark mode, so text rendered
with mode-aware tokens (light in dark mode) can vanish on it. Surfaces must use
``theme.Colors.*`` tokens; brand/auth and print CSS are exempt (not scanned
here). This catches the class of bug behind the "names/amounts disappeared"
report.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "nano_offline"

_PATTERN = re.compile(r"""bgcolor\s*=\s*["']#[0-9A-Fa-f]{3,8}["']""")

offenders: list[str] = []
scanned = 0
for base in (SRC / "components", SRC / "views"):
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        scanned += 1
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

assert not offenders, "hard-coded hex surface colours (use theme.Colors):\n" + "\n".join(offenders)
print(f"literal_surface_hex_contract passed ({scanned} files scanned)")