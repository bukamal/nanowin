from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nano_offline.core.router import Route, RouteDenied, Router

ROUTE_ALIASES = {
    "نقطة البيع": "pos",
    "بيع سريع": "pos",
    "جرد": "stocktake",
    "فاتورة بيع": "sale",
    "فاتورة شراء": "purchase",
}


class ShellRouteCoordinator:
    """Coordinate route rendering with the desktop shell's UI callbacks."""

    _DENY_MESSAGES = {
        "pos": "لا تملك صلاحية الفواتير",
        "sale": "لا تملك صلاحية الفواتير",
        "purchase": "لا تملك صلاحية الفواتير",
        "stocktake": "لا تملك صلاحية المواد/الجرد",
    }

    def __init__(
        self,
        *,
        active_key: Callable[[str], None],
        keyboard_handler: Callable[[], Any],
        set_header: Callable[[str, str], None],
        refresh_navigation: Callable[[], None],
        notify: Callable[[str], None],
        refresh_badge: Callable[[], None],
    ):
        self.active_key = active_key
        self.keyboard_handler = keyboard_handler
        self.set_header = set_header
        self.refresh_navigation = refresh_navigation
        self.notify = notify
        self.refresh_badge = refresh_badge

    def render(self, router: Router, page: Any, content: Any, key: str, kwargs: dict) -> None:
        route = router.get(key)
        self.active_key(key)
        page.on_keyboard_event = self.keyboard_handler()
        if route is not None and route.label:
            self.set_header(route.label, route.subtitle)
        self.refresh_navigation()
        content.opacity = 0
        try:
            if kwargs:
                route.render(**kwargs)
            else:
                route.render()
        except Exception as exc:
            content.opacity = 1
            try:
                content.update()
            except Exception:
                pass
            self.notify(f"تعذر فتح القسم: {exc}")
            raise
        content.opacity = 1
        content.update()
        self.refresh_badge()

    def on_denied(self, exc: Exception, key: str, _kwargs: dict) -> None:
        if isinstance(exc, RouteDenied) and "permission" in str(exc):
            self.notify(self._DENY_MESSAGES.get(key, "لا تملك صلاحية هذا القسم"))


class ShellNavigation:
    """Apply shell aliases and top-level navigation policy around a Router."""

    def __init__(
        self,
        router: Router,
        section_keys: set[str],
        *,
        open_notifications: Callable[[], None],
        on_notification_error: Callable[[Exception], None],
    ):
        self.router = router
        self.section_keys = section_keys
        self.open_notifications = open_notifications
        self.on_notification_error = on_notification_error

    def navigate(self, key: str, **kwargs: Any) -> None:
        if key in {"notifications", "إشعارات", "تنبيهات"}:
            try:
                self.open_notifications()
            except Exception as exc:
                self.on_notification_error(exc)
            return

        target = ROUTE_ALIASES.get(key, key)
        self.router.go(target, reset=target in self.section_keys, **kwargs)


def register_shell_routes(
    router: Router,
    *,
    page_meta: dict[str, tuple[str, str]],
    actions: dict[str, Callable],
    icon_map: dict[str, object],
    pos_render: Callable,
    sale_render: Callable,
    purchase_render: Callable,
    stocktake_render: Callable,
    pos_on_leave: Callable,
) -> Router:
    """Register section and workflow routes for the desktop shell."""
    for key, (title, subtitle) in page_meta.items():
        router.add(
            Route(
                key=key,
                label=title,
                subtitle=subtitle,
                icon=icon_map[key],
                permission=key,
                render=actions[key],
            )
        )
    router.register(
        "pos",
        label="نقطة البيع",
        permission="invoices",
        render=pos_render,
        on_leave=pos_on_leave,
    )
    router.register(
        "sale",
        label="فاتورة بيع",
        subtitle="إنشاء فاتورة بيع جديدة — النقدي افتراضيًا",
        permission="invoices",
        render=sale_render,
        replace_current=True,
    )
    router.register(
        "purchase",
        label="فاتورة شراء",
        subtitle="إنشاء فاتورة شراء جديدة — النقدي افتراضيًا",
        permission="invoices",
        render=purchase_render,
        replace_current=True,
    )
    router.register(
        "stocktake",
        label="الجرد",
        subtitle="جرد بالمسح المستمر",
        permission="items",
        render=stocktake_render,
    )
    return router


__all__ = ["ROUTE_ALIASES", "ShellNavigation", "ShellRouteCoordinator", "register_shell_routes"]
