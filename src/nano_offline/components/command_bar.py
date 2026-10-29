"""Top ribbon: the app's single navigation surface.

Layout (top to bottom), mirroring a classic desktop ERP:

* **menu bar** -- one button per section (icon over label). Clicking a button
  opens a **dropdown** of that section's commands (the section entry itself +
  its contextual actions) instead of navigating immediately.
* **document tabs** -- every section you open appears as a tab (with a close
  button) under the menu bar; clicking a tab re-activates that section.
* **command strip** -- the contextual command buttons for the active tab.

The standalone "secondary" buttons that used to live in a second ribbon row are
now folded into each section's dropdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import flet as ft

from nano_offline.core import branding
from nano_offline.core.theme import Colors, Radius, Shadow


@dataclass
class RibbonAction:
    key: str
    label: str
    icon: str
    on_click: Callable | None = None
    # Submenu entries: (label, icon, on_click)
    items: list[tuple[str, str, Callable]] | None = None
    # Primary-accented action (the one obvious "do the thing" button).
    accent: bool = False
    tooltip: str | None = None


@dataclass
class RibbonTab:
    key: str
    label: str
    icon: str


@dataclass
class RibbonMenu:
    """A top menu button: its dropdown lists ``items`` = (label, icon, on_click)."""

    key: str
    label: str
    icon: str
    items: list[tuple[str, str, Callable]] = field(default_factory=list)


class CommandBar(ft.Container):
    def __init__(
        self,
        *,
        page: ft.Page | None = None,
        menus: list[RibbonMenu] | None = None,
        tabs: list[RibbonTab] | None = None,
        titles: dict | None = None,
        on_open: Callable | None = None,
        on_close: Callable | None = None,
        brand_title: str = branding.APP_BRAND,
        brand_subtitle: str = branding.APP_TAGLINE,
        user_name: str = "",
        user_sub: str = "",
        on_logout: Callable | None = None,
        on_search: Callable | None = None,
        extra: list[ft.Control] | None = None,
    ):
        # ``tabs`` is kept for backward compatibility (a menu with no items).
        self.menus = list(menus or [])
        self._titles: dict[str, tuple[str, str]] = dict(
            titles or {t.key: (t.label, t.icon) for t in (tabs or [])}
        )
        self.on_open = on_open
        self.on_close = on_close
        self.brand_title = brand_title
        self.brand_subtitle = brand_subtitle
        self.user_name = user_name
        self.user_sub = user_sub
        self.on_logout = on_logout
        self.on_search = on_search
        self.extra = list(extra or [])

        self._active = {"key": None}
        self._docs: dict[str, ft.Container] = {}

        self._actions_row = ft.Row([], spacing=6, scroll=ft.ScrollMode.AUTO, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=False)
        self._actions_wrap = ft.Container(self._actions_row, visible=False)
        self._docs_row = ft.Row([], spacing=4, scroll=ft.ScrollMode.AUTO, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self._docs_wrap = ft.Container(
            self._docs_row,
            visible=False,
            padding=ft.padding.only(top=4, bottom=2),
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
        )

        self._menu_row = ft.Row([], spacing=2, scroll=ft.ScrollMode.AUTO, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        for menu in self.menus:
            self._menu_row.controls.append(self._menu_button(menu))

        avatar_text = "".join(p[:1] for p in (self.user_name or "N").split()[:2]).upper() or "N"
        brand = ft.Row(
            [
                ft.Image(src="icon.png", width=36, height=36, fit=ft.ImageFit.CONTAIN),
                ft.Column(
                    [
                        ft.Text(self.brand_title, size=15, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                        ft.Text(self.brand_subtitle, size=9, color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=0,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        top = ft.Row(
            [
                brand,
                ft.VerticalDivider(width=1, thickness=1, color=Colors.BORDER),
                self._menu_row,
                ft.Container(expand=True),
                self._search_button(),
                *self.extra,
                ft.VerticalDivider(width=1, thickness=1, color=Colors.BORDER),
                ft.Container(
                    ft.Text(avatar_text, color=Colors.WHITE, weight=ft.FontWeight.BOLD, size=12),
                    width=34, height=34, alignment=ft.alignment.center, bgcolor=Colors.PRIMARY, border_radius=11,
                ),
                ft.Column(
                    [
                        ft.Text(self.user_name, size=11, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                        ft.Text(self.user_sub, size=9, color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=0,
                ),
                ft.IconButton(icon=ft.Icons.LOGOUT, tooltip="تسجيل الخروج", icon_size=19,
                              icon_color=Colors.TEXT_SECONDARY, on_click=lambda _: self.on_logout and self.on_logout()),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        super().__init__(
            content=ft.Column([top, self._docs_wrap, self._actions_wrap], spacing=0),
            bgcolor=Colors.WHITE,
            padding=ft.padding.symmetric(horizontal=14, vertical=8),
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        self.set_active(self._active["key"])

    # ----------------------------------------------------------- menu buttons
    def _menu_button(self, menu: RibbonMenu) -> ft.Control:
        """A top menu button (icon + name) opening a real dropdown of its
        commands. Uses PopupMenuButton (stable across Flet versions)."""
        box = ft.Container(
            ft.Row(
                [
                    ft.Icon(menu.icon, size=18, color=Colors.TEXT_SECONDARY),
                    ft.Text(menu.label, size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_PRIMARY),
                    ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=16, color=Colors.TEXT_FAINT),
                ],
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            border_radius=Radius.SM,
            ink=True,
        )
        items: list[ft.Control] = []
        for entry in menu.items:
            if entry is None:
                continue
            label, item_icon, cb = entry
            items.append(
                ft.PopupMenuItem(
                    text=label,
                    icon=item_icon,
                    on_click=(lambda _, c=cb: c()) if cb is not None else None,
                )
            )
        return ft.PopupMenuButton(content=box, tooltip=menu.label, items=items)

    # ------------------------------------------------------- document tabs
    def open_tab(self, key: str) -> None:
        """Show/activate the tab for a route (no-op for unknown routes)."""
        if key not in self._titles:
            return
        if key not in self._docs:
            label_text, icon_name = self._titles[key]
            icon = ft.Icon(icon_name, size=15, color=Colors.TEXT_SECONDARY)
            label = ft.Text(label_text, size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_MUTED_DARK)
            close = ft.Container(
                ft.Icon(ft.Icons.CLOSE, size=13, color=Colors.TEXT_FAINT),
                width=18,
                height=18,
                alignment=ft.alignment.center,
                border_radius=Radius.SM,
                on_click=lambda _, k=key: self.close_tab(k),
                ink=True,
            )
            box = ft.Container(
                ft.Row([icon, label, close], spacing=6, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.padding.only(left=12, right=6, top=3, bottom=3),
                border_radius=Radius.SM,
                border=ft.border.all(1, Colors.BORDER),
                on_click=lambda _, k=key: self._activate(k),
                ink=True,
            )
            self._docs[key] = box
            self._docs_row.controls.append(box)
            self._docs_wrap.visible = True
        self.set_active(key)

    def close_tab(self, key: str, *, silent: bool = False) -> None:
        box = self._docs.pop(key, None)
        if box is None:
            return
        try:
            self._docs_row.controls.remove(box)
        except Exception:
            pass
        self._docs_wrap.visible = bool(self._docs_row.controls)
        if self._active.get("key") == key and self.on_close and not silent:
            try:
                self.on_close(key)
            except Exception:
                pass
        self._safe_update(self)

    def close_all_tabs(self) -> None:
        self._docs.clear()
        self._docs_row.controls = []
        self._docs_wrap.visible = False
        self._safe_update(self)

    def _activate(self, key: str) -> None:
        self.open_tab(key)
        if self.on_open:
            try:
                self.on_open(key)
            except Exception:
                pass

    def set_active(self, key: str | None) -> None:
        self._active["key"] = key
        for k, box in self._docs.items():
            active = k == key
            row = box.content
            icon, label = row.controls[0], row.controls[1]
            box.bgcolor = Colors.PRIMARY_BG if active else None
            box.border = ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER)
            icon.color = Colors.PRIMARY if active else Colors.TEXT_SECONDARY
            label.color = Colors.PRIMARY if active else Colors.TEXT_MUTED_DARK
            label.weight = ft.FontWeight.BOLD if active else ft.FontWeight.W_600
        self._safe_update(self)

    # -------------------------------------------------------- contextual strip
    def set_actions(self, actions: list[RibbonAction]) -> None:
        self._actions_row.controls = [self._action_button(a) for a in actions]
        self._actions_wrap.visible = bool(actions)
        self._safe_update(self)

    def _action_button(self, action: RibbonAction) -> ft.Control:
        icon_color = Colors.WHITE if action.accent else Colors.TEXT_MUTED_DARK
        text_color = Colors.WHITE if action.accent else Colors.TEXT_PRIMARY
        inner = ft.Row(
            [
                ft.Icon(action.icon, size=16, color=icon_color),
                ft.Text(action.label, size=12, weight=ft.FontWeight.W_600, color=text_color),
                *([ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=16, color=icon_color)] if action.items else []),
            ],
            spacing=6,
            tight=True,
        )
        box = ft.Container(
            inner,
            padding=ft.padding.symmetric(horizontal=12, vertical=7),
            border_radius=Radius.SM,
            border=ft.border.all(1, Colors.PRIMARY if action.accent else Colors.BORDER),
            bgcolor=Colors.PRIMARY if action.accent else Colors.WHITE,
            ink=True,
        )
        if action.items:
            return ft.PopupMenuButton(
                content=box,
                tooltip=action.tooltip or action.label,
                items=[
                    ft.PopupMenuItem(text=label, icon=icon, on_click=(lambda _, cb=cb: cb()) if cb else None)
                    for (label, icon, cb) in action.items
                ],
            )
        return ft.Container(
            box,
            on_click=lambda _, cb=action.on_click: cb() if cb else None,
            tooltip=action.tooltip or action.label,
        )

    def _search_button(self) -> ft.Control:
        return ft.Container(
            ft.Icon(ft.Icons.SEARCH, size=18, color=Colors.TEXT_SECONDARY),
            width=38,
            height=38,
            alignment=ft.alignment.center,
            border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.SM,
            ink=True,
            tooltip="بحث سريع (Ctrl+K)",
            on_click=lambda _: self.on_search and self.on_search(),
        )

    @staticmethod
    def _safe_update(control: ft.Control) -> None:
        try:
            control.update()
        except Exception:
            pass


__all__ = ["CommandBar", "RibbonTab", "RibbonAction", "RibbonMenu"]
