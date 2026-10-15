"""Root-level entry point for Flet Windows build.

Flet's Windows bootstrap runs runpy.run_module("main") against the extracted
app.zip. This file must live at the project root so Flet finds it at zip root.

It bootstraps sys.path from src/ then exec's the real main.py in this module's
namespace so ft.app(target=main) runs here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_exec_path = Path(__file__).parent / "src" / "main.py"
with open(_exec_path, "r", encoding="utf-8") as _f:
    exec(compile(_f.read(), str(_exec_path), "exec"), globals())
