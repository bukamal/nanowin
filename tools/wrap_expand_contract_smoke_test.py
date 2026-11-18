"""Contract: never put an ``expand=True`` child inside an ``ft.Row(wrap=True)``.

Flutter's ``Wrap`` cannot lay out an ``Expanded`` child and paints a blank grey
box over the whole area instead. This has bitten several screens (restaurant
boards, the parties bulk sheet toolbar, the finance vouchers toolbar) and is
easy to reintroduce. This static scan fails the gate on any such Row.

Pure-AST, no flet import needed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _row_has_wrap(call: ast.Call) -> bool:
    return any(kw.arg == "wrap" and _is_true(kw.value) for kw in call.keywords)


def _child_has_expand(call: ast.Call) -> bool:
    return any(kw.arg == "expand" and _is_true(kw.value) for kw in call.keywords)


offenders: list[str] = []
scanned = 0
for path in SRC.rglob("*.py"):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    scanned += 1
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "Row"):
            continue
        if not _row_has_wrap(node):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        if any(isinstance(el, ast.Call) and _child_has_expand(el) for el in node.args[0].elts):
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

assert not offenders, (
    "ft.Row(wrap=True) cannot host an expand=True child (renders a grey box): "
    + ", ".join(offenders)
)
print(f"wrap_expand_contract passed ({scanned} files scanned)")
