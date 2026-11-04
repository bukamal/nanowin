"""Enter navigation for the app's form sheets.

``wire_enter_navigation`` makes Enter step through a form's text fields and
submit from the last one, matching the editable grids' one-step-at-a-time
behavior. Non-field controls (rows, columns, search selects) are ignored.
"""

from __future__ import annotations

import flet as ft

from nano_offline.components.form_sheet import wire_enter_navigation


def test_enter_steps_through_fields_and_submits_from_the_last():
    saved = []
    first = ft.TextField()
    second = ft.TextField()
    third = ft.TextField()

    wire_enter_navigation([first, second, third], lambda _=None: saved.append(True))

    assert first.on_submit is not None
    assert second.on_submit is not None
    assert third.on_submit is not None
    third.on_submit(None)  # last field submits
    assert saved == [True]


def test_existing_on_submit_is_preserved():
    called = []
    first = ft.TextField(on_submit=lambda _e: called.append("own"))
    last = ft.TextField()

    wire_enter_navigation([first, last], lambda _=None: called.append("save"))

    first.on_submit(None)
    assert called == ["own"]
    last.on_submit(None)
    assert called == ["own", "save"]


def test_nested_controls_are_traversed_in_reading_order():
    saved = []
    a = ft.TextField()
    b = ft.TextField()
    c = ft.TextField()

    wire_enter_navigation([a, ft.Row([b, c])], lambda _=None: saved.append(True))

    assert a.on_submit is not None
    assert b.on_submit is not None
    c.on_submit(None)
    assert saved == [True]


def test_single_field_form_submits_on_enter():
    saved = []
    only = ft.TextField()

    wire_enter_navigation([only], lambda _=None: saved.append(True))

    only.on_submit(None)
    assert saved == [True]


class _Composite(ft.Column):
    def __init__(self):
        self.inner = ft.TextField()
        super().__init__([self.inner])


def test_composite_widgets_are_not_traversed():
    saved = []
    a = ft.TextField()
    composite = _Composite()

    wire_enter_navigation([a, composite], lambda _=None: saved.append(True))

    assert composite.inner.on_submit is None  # left to its own key handling
    a.on_submit(None)                        # sole real field -> submit
    assert saved == [True]
