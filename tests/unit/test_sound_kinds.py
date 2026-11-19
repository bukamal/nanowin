"""Sound-cue catalogue: the data-entry kinds are registered with their own tones."""

from __future__ import annotations

from pathlib import Path

from nano_offline.core import sound, sound_settings


def test_data_entry_kinds_are_registered_with_their_own_assets():
    assert "line_add" in sound._SOUND_KINDS
    assert "pick" in sound._SOUND_KINDS
    # These no longer borrow the 'scan' blip -- each resolves to its own .wav.
    assert sound._ASSET_FOR.get("line_add", "line_add") == "line_add"
    assert sound._ASSET_FOR.get("pick", "pick") == "pick"
    sounds = Path(sound.__file__).resolve().parents[2] / "assets" / "sounds"
    assert (sounds / "line_add.wav").is_file()
    assert (sounds / "pick.wav").is_file()


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
