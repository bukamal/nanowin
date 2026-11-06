"""Sound-cue catalogue: the two data-entry kinds are registered and gated."""

from __future__ import annotations

from nano_offline.core import sound, sound_settings


def test_data_entry_kinds_are_registered_and_borrow_the_scan_asset():
    assert "line_add" in sound._SOUND_KINDS
    assert "pick" in sound._SOUND_KINDS
    assert sound._ASSET_FOR["line_add"] == "scan"
    assert sound._ASSET_FOR["pick"] == "scan"


def test_data_entry_kinds_default_on_and_can_be_disabled():
    settings: dict[str, str] = {}
    assert sound_settings.kind_enabled(settings, "line_add") is True
    assert sound_settings.kind_enabled(settings, "pick") is True

    settings[sound_settings.KIND_LINE_ADD_KEY] = "0"
    settings[sound_settings.KIND_PICK_KEY] = "0"
    assert sound_settings.kind_enabled(settings, "line_add") is False
    assert sound_settings.kind_enabled(settings, "pick") is False

def test_cell_commit_kind_is_registered_and_gated():
    assert "cell" in sound._SOUND_KINDS
    settings: dict[str, str] = {}
    assert sound_settings.kind_enabled(settings, "cell") is True
    settings[sound_settings.KIND_CELL_KEY] = "0"
    assert sound_settings.kind_enabled(settings, "cell") is False
