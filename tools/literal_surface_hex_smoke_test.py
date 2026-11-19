"""Contract: no hard-coded hex colours in components/views.

A literal ``color="#RRGGBB"`` / ``bgcolor="#RRGGBB"`` does not follow dark
mode, so text or surfaces rendered with mode-aware tokens can vanish on it.
Surfaces and text should use ``theme.Colors.*`` tokens. A few files own a
deliberately fixed palette (drawing a scene / a camera overlay) and are
allow-listed. Print CSS, LAN web pages and branding gradients live outside
components/views and are not scanned here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "nano_offline"

#: Files with a deliberate, mode-independent fixed palette.
_ALLOWED = {
    "store_scene.py",          # home scene drawing (own dark/light branches)
    "smart_barcode_scanner.py",  # camera overlay: always-dark chrome
}

_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
# Attributes that actually paint a surface or text (gradient stops etc. too).
_ATTR = re.compile(r"""(?:color|bgcolor|border_color|shadow_color|fill|stroke|colors?)\s*[=(]""")

offenders: list[str] = []
scanned = 0
for base in (SRC / "components", SRC / "views"):
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts or path.name in _ALLOWED:
            continue
        scanned += 1
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _HEX.search(line) and _ATTR.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

assert not offenders, "hard-coded hex colours found (use theme.Colors tokens):\n" + "\n".join(offenders)
print(f"literal_surface_hex_contract passed ({scanned} files scanned)")