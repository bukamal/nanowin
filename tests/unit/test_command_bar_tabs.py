from __future__ import annotations

from types import SimpleNamespace

from nano_offline.components.command_bar import CommandBar


def _bar(active: str, keys: list[str], history: list[str]) -> tuple[SimpleNamespace, list[str], list[str]]:
    opened: list[str] = []
    closed: list[str] = []
    bar = SimpleNamespace(
        _docs={key: object() for key in keys},
        _docs_row=SimpleNamespace(controls=[]),
        _docs_wrap=SimpleNamespace(visible=True),
        _active={"key": active},
        _tab_history=list(history),
        _safe_update=lambda _control: None,
        on_open=opened.append,
        on_close=closed.append,
    )
    bar._docs_row.controls = list(bar._docs.values())

    def set_active(key: str | None) -> None:
        bar._active["key"] = key
        if key in bar._docs:
            bar._tab_history = [tab for tab in bar._tab_history if tab != key]
            bar._tab_history.append(key)

    bar.set_active = set_active
    return bar, opened, closed


def test_closing_active_tab_activates_most_recent_remaining_tab():
    bar, opened, closed = _bar(
        "reports",
        ["items", "customers", "reports"],
        ["items", "customers", "reports"],
    )

    CommandBar.close_tab(bar, "reports")

    assert bar._active["key"] == "customers"
    assert opened == ["customers"]
    assert closed == []
    assert set(bar._docs) == {"items", "customers"}


def test_closing_inactive_tab_keeps_current_tab_active():
    bar, opened, closed = _bar(
        "reports",
        ["items", "reports"],
        ["items", "reports"],
    )

    CommandBar.close_tab(bar, "items")

    assert bar._active["key"] == "reports"
    assert opened == []
    assert closed == []


def test_closing_last_active_tab_returns_to_home_callback():
    bar, opened, closed = _bar("reports", ["reports"], ["reports"])

    CommandBar.close_tab(bar, "reports")

    assert bar._active["key"] is None
    assert bar._docs_wrap.visible is False
    assert opened == []
    assert closed == ["reports"]
