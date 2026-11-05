"""UI contract tests: lock the unified button language and Enter wiring.

Two layers:
* a functional check that ``render_form_sheet`` builds a footer with the
  shared primary/secondary buttons and wires Enter on its fields;
* a source-level contract that the views no longer hand-roll danger/delete
  filled buttons (they must go through ``danger_button``).

These guard exactly the regressions the button-unification work fixed.
"""

from __future__ import annotations

from pathlib import Path

import flet as ft

from nano_offline.components import buttons as B
from nano_offline.components.form_sheet import render_form_sheet

_ROOT = Path(__file__).resolve().parents[2]
_VIEWS = _ROOT / "src" / "nano_offline" / "views"


class _FakePage:
    width = 1280
    height = 800

    def update(self, *a, **k):
        pass


def _walk(control):
    yield control
    for child in getattr(control, "controls", None) or ():
        yield from _walk(child)
    content = getattr(control, "content", None)
    if content is not None:
        yield from _walk(content)


def test_render_form_sheet_uses_shared_footer_and_wires_enter():
    sheet = ft.BottomSheet(content=ft.Container())
    field = ft.TextField()
    render_form_sheet(
        _FakePage(), sheet,
        title="عنوان", fields=[field],
        on_close=lambda *_: None, on_save=lambda *_: None,
    )

    # Enter on the (single) field triggers the save.
    assert field.on_submit is not None

    nodes = list(_walk(sheet.content))
    filled = [c for c in nodes if isinstance(c, ft.FilledButton)]
    outlined = [c for c in nodes if isinstance(c, ft.OutlinedButton)]

    # The primary save button uses the shared geometry + busy-capable content.
    assert filled, "no primary button in the form footer"
    assert any(isinstance(b.content, ft.Row) and getattr(b.content, "data", None) for b in filled)
    assert all(b.height == B.BTN_HEIGHT_LG for b in filled)
    assert outlined, "no secondary (cancel) button in the form footer"
    assert all(b.height == B.BTN_HEIGHT_LG for b in outlined)


def test_views_do_not_hand_roll_danger_or_delete_filled_buttons():
    offenders: list[str] = []
    for path in _VIEWS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if 'ft.FilledButton("حذف"' in text or 'ft.FilledButton("حذف المحدد"' in text:
            offenders.append(f"{path.name}: raw delete FilledButton")
        # A danger-styled filled button inline (should be danger_button()).
        for line in text.splitlines():
            if "FilledButton(" in line and "Colors.DANGER" in line:
                offenders.append(f"{path.name}: inline DANGER FilledButton")
    assert offenders == [], f"unified button contract violated: {offenders}"


def test_form_sheet_still_delegates_to_the_shared_factories():
    text = (_ROOT / "src" / "nano_offline" / "components" / "form_sheet.py").read_text(encoding="utf-8")
    assert "footer_actions" in text
    assert "primary_button" in text