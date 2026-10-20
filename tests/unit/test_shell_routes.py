from __future__ import annotations

from types import SimpleNamespace

import pytest

from nano_offline.core.router import RouteDenied, Router
from nano_offline.core.shell_routes import (
    ROUTE_ALIASES,
    ShellNavigation,
    ShellRouteCoordinator,
    register_shell_routes,
)


def test_shell_route_registration_preserves_permissions_and_aliases():
    router = Router(render=lambda _key, _kwargs: None)
    dashboard = lambda: None
    pos = lambda: None
    sale = lambda: None
    purchase = lambda: None
    stocktake = lambda: None
    leave = lambda _key, _kwargs: None

    register_shell_routes(
        router,
        page_meta={"dashboard": ("الرئيسية", "ملخص")},
        actions={"dashboard": dashboard},
        icon_map={"dashboard": "home"},
        pos_render=pos,
        sale_render=sale,
        purchase_render=purchase,
        stocktake_render=stocktake,
        pos_on_leave=leave,
    )

    assert router.get("dashboard").render is dashboard
    assert router.get("dashboard").permission == "dashboard"
    assert router.get("pos").permission == "invoices"
    assert router.get("pos").on_leave is leave
    assert router.get("sale").replace_current is True
    assert router.get("purchase").replace_current is True
    assert router.get("stocktake").permission == "items"
    assert ROUTE_ALIASES["بيع سريع"] == "pos"


def test_shell_navigation_applies_aliases_and_resets_top_level_sections():
    rendered = []
    router = Router(render=lambda key, kwargs: rendered.append((key, kwargs)))
    router.register("dashboard", render=lambda: None)
    router.register("pos", render=lambda: None)
    router.register("items", render=lambda: None)
    navigation = ShellNavigation(
        router,
        {"dashboard", "items"},
        open_notifications=lambda: None,
        on_notification_error=lambda exc: None,
    )

    navigation.navigate("dashboard")
    navigation.navigate("بيع سريع")
    navigation.navigate("items", prefill_barcode="123")

    assert rendered == [
        ("dashboard", {}),
        ("pos", {}),
        ("items", {"prefill_barcode": "123"}),
    ]
    assert router.path == ["items"]


def test_shell_navigation_routes_notifications_without_changing_history():
    opened = []
    errors = []
    router = Router(render=lambda _key, _kwargs: None)
    router.register("dashboard", render=lambda: None)
    router.go("dashboard", reset=True)
    navigation = ShellNavigation(
        router,
        {"dashboard"},
        open_notifications=lambda: opened.append(True),
        on_notification_error=errors.append,
    )

    navigation.navigate("تنبيهات")

    assert opened == [True]
    assert errors == []
    assert router.path == ["dashboard"]


def test_route_coordinator_preserves_shell_render_lifecycle():
    calls = []
    page = type("Page", (), {})()
    content = type("Content", (), {"opacity": 1, "update": lambda self: calls.append("update")})()
    router = Router(render=lambda _key, _kwargs: None)
    router.register("items", label="المواد", subtitle="المخزون", render=lambda **kwargs: calls.append(kwargs))
    coordinator = ShellRouteCoordinator(
        active_key=lambda key: calls.append(("active", key)),
        keyboard_handler=lambda: "shell-key-handler",
        set_header=lambda title, subtitle: calls.append(("header", title, subtitle)),
        refresh_navigation=lambda: calls.append("navigation"),
        notify=calls.append,
        refresh_badge=lambda: calls.append("badge"),
    )

    coordinator.render(router, page, content, "items", {"prefill_barcode": "123"})

    assert calls == [
        ("active", "items"),
        ("header", "المواد", "المخزون"),
        "navigation",
        {"prefill_barcode": "123"},
        "update",
        "badge",
    ]
    assert page.on_keyboard_event == "shell-key-handler"
    assert content.opacity == 1


def test_route_coordinator_restores_content_and_reports_render_errors():
    calls = []
    content = type("Content", (), {"opacity": 1, "update": lambda self: calls.append("update")})()
    router = Router(render=lambda _key, _kwargs: None)

    def fail():
        raise ValueError("broken screen")

    router.register("items", render=fail)
    coordinator = ShellRouteCoordinator(
        active_key=lambda _key: None,
        keyboard_handler=lambda: None,
        set_header=lambda _title, _subtitle: None,
        refresh_navigation=lambda: None,
        notify=calls.append,
        refresh_badge=lambda: calls.append("badge"),
    )

    with pytest.raises(ValueError, match="broken screen"):
        coordinator.render(router, SimpleNamespace(), content, "items", {})

    assert content.opacity == 1
    assert calls == ["update", "تعذر فتح القسم: broken screen"]


def test_route_coordinator_only_notifies_permission_denials():
    messages = []
    coordinator = ShellRouteCoordinator(
        active_key=lambda _key: None,
        keyboard_handler=lambda: None,
        set_header=lambda _title, _subtitle: None,
        refresh_navigation=lambda: None,
        notify=messages.append,
        refresh_badge=lambda: None,
    )

    coordinator.on_denied(RouteDenied("no permission for route: pos"), "pos", {})
    coordinator.on_denied(RouteDenied("route not registered: unknown"), "unknown", {})

    assert messages == ["لا تملك صلاحية الفواتير"]
