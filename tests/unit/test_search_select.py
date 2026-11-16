"""Unit tests for SearchSelect's searchable vs dropdown modes."""

from __future__ import annotations


def test_search_select_dropdown_mode_routes_value_and_change():
    from nano_offline.components.search_select import SearchSelect

    seen: list = []
    sel = SearchSelect(
        label="الشكل",
        choices=[("standard", "قياسي"), ("soft", "ناعم")],
        value="soft",
        allow_clear=False,
        dropdown=True,
        on_change=lambda e: seen.append(e.control.value),
    )
    assert sel.dropdown is not None
    assert sel.value == "soft"
    assert sel.dropdown.value == "soft"

    # A user pick updates the machine value, then calls the callback with a
    # Flet-like event whose .control.value is the chosen key.
    sel.dropdown.value = "standard"
    sel._dropdown_changed()
    assert sel.value == "standard"
    assert seen == ["standard"]

    # label / set_choices route to the underlying Dropdown.
    sel.label = "المظهر"
    assert sel.dropdown.label == "المظهر"
    sel.set_choices([("a", "أ"), ("b", "ب")], preserve_value=False)
    assert sel.value is None
    assert [o.key for o in sel.dropdown.options] == ["a", "b"]


def test_search_select_dropdown_clear_option_when_optional():
    from nano_offline.components.search_select import SearchSelect

    sel = SearchSelect(label="طابعة بديلة", choices=[("1", "المطبخ")], dropdown=True)
    # allow_clear defaults True -> a leading blank option exists.
    assert sel.dropdown.options[0].key == ""
    sel.dropdown.value = ""
    sel._dropdown_changed()
    assert sel.value is None


def test_search_select_default_mode_still_searchable():
    from nano_offline.components.search_select import SearchSelect

    sel = SearchSelect(label="عميل", choices=[("1", "أحمد")], value="1")
    assert sel.dropdown is None
    assert sel.value == "1"
    assert sel.field.value == "أحمد"
