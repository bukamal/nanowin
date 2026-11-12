"""Kitchen/tables boards: pure helpers (age label/colour)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nano_offline.components.restaurant_boards import _age_color, _minutes_since
from nano_offline.core.theme import Colors


def test_minutes_since_parses_sqlite_timestamp():
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=7)).strftime("%Y-%m-%d %H:%M:%S")
    minutes = _minutes_since(stamp)
    assert minutes is not None and 6.5 <= minutes <= 8.0


def test_minutes_since_handles_garbage():
    assert _minutes_since("") is None
    assert _minutes_since("not-a-date") is None


def test_age_color_thresholds():
    assert _age_color(None) == Colors.TEXT_SECONDARY
    assert _age_color(5) == Colors.SUCCESS_DARK
    assert _age_color(15) == Colors.WARNING_DARKER
    assert _age_color(45) == Colors.DANGER