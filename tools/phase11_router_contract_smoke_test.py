"""Router contract smoke: navigation moved out of ad-hoc closures into core/router.py.

Regression guard for the "gray screen after login" incident and for the v0.18
central-router refactor. Checks two independent things:

  1. The pure Router is importable and its stack semantics behave (this is the
     real test; also mirrored in tests/unit/test_router.py).
  2. The shell route coordinator owns route registration, rendering, and
     navigation, while main.py wires it to the desktop shell and Android back.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nano_offline.core.router import RouteDenied, Router  # noqa: E402

# ---- (1) stack semantics ---------------------------------------------------
renders: list[tuple[str, dict]] = []
router = Router(home="dashboard", render=lambda key, kwargs: renders.append((key, dict(kwargs))))
for key in ("dashboard", "items", "invoices"):
    router.register(key, label=key, render=lambda **kw: None)
router.register("sale", permission="invoices", render=lambda **kw: None)

assert router.go("invoices") is True
assert router.go("sale") is True and router.path == ["invoices", "sale"]
assert router.back() is True and router.current == "invoices"
assert router.go("items", reset=True) is True and router.path == ["items"]  # tab tap clears stack
assert router.back() is False  # at root
assert router.go("ghost") is False  # unknown route denied, nothing rendered
assert renders[-1][0] == "items"  # ghost never rendered

seen_denied: list[str] = []
router.can_render = lambda route: route.key != "sale"
router.on_denied = lambda exc, key, kw: seen_denied.append(key)
assert router.go("sale") is False and seen_denied == ["sale"]
assert isinstance(RouteDenied("x"), Exception)

# a failing view must undo its own push (no half-rendered screen on the stack)
def _boom_render(key, kwargs):
    if key == "items":
        raise RuntimeError("view crashed")

router.can_render = None
router.render = _boom_render
router.go("invoices")
before = router.path
assert router.go("items") is False and router.path == before
router.render = lambda key, kwargs: renders.append((key, dict(kwargs)))

# ---- (2) main.py wiring ----------------------------------------------------
main = (ROOT / "src/main.py").read_text(encoding="utf-8")
shell_routes = (ROOT / "src/nano_offline/core/shell_routes.py").read_text(encoding="utf-8")
for needle in [
    "from nano_offline.core.router import",
    "router = Router(",
    "register_shell_routes(",
    "ShellRouteCoordinator(",
    "router.can_go_back()",
    "router.back()",
]:
    assert needle in main, f"main.py no longer wires the Router: {needle}"
for needle in [
    "router.register(",
    "self.router.go(target, reset=target in self.section_keys",
    "def render(self, router: Router",
    "def on_denied(",
]:
    assert needle in shell_routes, f"shell_routes.py no longer coordinates routes: {needle}"
# The gray-screen guard: build_shell failures surface a dialog instead of an
# empty cleared page.
assert "تعذر تحميل الواجهة" in main, "open_shell must not leave a silent blank page"
assert "build_shell(page, ctx" in main

# ft.app() must stay unconditional at module level (the Android runtime imports
# this file as a regular module and relies on the embedded launch call to
# start the app). A guard around it ships an APK that renders nothing.
assert "ft.app(target=main)" in main, "module-level ft.app() launch removed"
assert 'if __name__' not in main, "a module-guard around ft.app() breaks the APK (gray screen)"

print("phase11_router_contract_smoke_test passed")
