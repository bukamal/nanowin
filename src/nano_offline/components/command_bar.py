"""Top ribbon: the app's single navigation surface.

Replaces the old 280px desktop sidebar. Row one is the brand + the primary
section tabs (icon **and** label, so it reads at a glance); row two is the
*contextual* action strip -- the buttons relevant to whatever section is
open (e.g. items: "مادة جديدة", "تصنيفات ووحدات ▾"). Groups with children
render as a popup ("submenu"), matching the familiar ribbon behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
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


class CommandBar(ft.Container):
    def __init__(
        self,
        *,
        page: ft.Page | None = None,
        tabs: list[RibbonTab] | None = None,
        on_tab: Callable | None = None,
        brand_title: str = branding.APP_BRAND,
        brand_subtitle: str = branding.APP_TAGLINE,
        user_name: str = "",
        user_sub: str = "",
        on_logout: Callable | None = None,
        on_search: Callable | None = None,
        extra: list[ft.Control] | None = None,
    ):
        self.tabs = list(tabs or [])
        self.on_tab = on_tab
        self.brand_title = brand_title
        self.brand_subtitle = brand_subtitle
        self.user_name = user_name
        self.user_sub = user_sub
        self.on_logout = on_logout
        self.on_search = on_search
        self.extra = list(extra or [])

        self._active = {"key": self.tabs[0].key if self.tabs else None}
        self._tab_boxes: dict[str, ft.Container] = {}
        self._actions_row = ft.Row([], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=False)
        self._actions_wrap = ft.Container(self._actions_row, visible=False)
        self._tabs_row = ft.Row([], spacing=4, scroll=ft.ScrollMode.AUTO, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        for tab in self.tabs:
            box = self._tab_box(tab)
            self._tab_boxes[tab.key] = box
            self._tabs_row.controls.append(box)

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
                self._tabs_row,
                ft.Container(expand=True),
                *self.extra,
                self._search_button(),
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
            content=ft.Column([top, self._actions_wrap], spacing=0),
            bgcolor=Colors.WHITE,
            padding=ft.padding.symmetric(horizontal=14, vertical=8),
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
            shadow=Shadow.SM,
        )
        self.set_active(self._active["key"])

    # ------------------------------------------------------------- tab chrome
    def _tab_box(self, tab: RibbonTab) -> ft.Container:
        icon = ft.Icon(tab.icon, size=20, color=Colors.TEXT_SECONDARY)
        text = ft.Text(tab.label, size=11, weight=ft.FontWeight.W_600, color=Colors.TEXT_MUTED_DARK)
        return ft.Container(
            ft.Column([icon, text], spacing=3, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.padding.symmetric(horizontal=12, vertical=6),
            border_radius=Radius.SM,
            border=ft.border.all(1, Colors.BORDER),
            on_click=lambda _, k=tab.key: self.on_tab and self.on_tab(k),
            ink=True,
        )

    def set_active(self, key: str | None) -> None:
        self._active["key"] = key
        for tab_key, box in self._tab_boxes.items():
            active = tab_key == key
            row = box.content
            icon, text = row.controls[0], row.controls[1]
            box.bgcolor = Colors.PRIMARY_BG if active else None
            box.border = ft.border.all(1, Colors.PRIMARY_BORDER if active else Colors.BORDER)
            icon.color = Colors.PRIMARY if active else Colors.TEXT_SECONDARY
            text.color = Colors.PRIMARY if active else Colors.TEXT_MUTED_DARK
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
            ft.Row(
                [
                    ft.Icon(ft.Icons.SEARCH, size=16, color=Colors.TEXT_SECONDARY),
                    ft.Text("بحث سريع", size=12, color=Colors.TEXT_SECONDARY),
                    ft.Container(
                        ft.Text("Ctrl K", size=9, color=Colors.TEXT_FAINT, weight=ft.FontWeight.BOLD),
                        padding=ft.padding.symmetric(horizontal=6, vertical=2),
                        bgcolor=Colors.BACKGROUND_ALT,
                        border_radius=6,
                        border=ft.border.all(1, Colors.BORDER),
                    ),
                ],
                spacing=7,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=10, vertical=7),
            border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.SM,
            ink=True,
            on_click=lambda _: self.on_search and self.on_search(),
        )

    @staticmethod
    def _safe_update(control: ft.Control) -> None:
        try:
            control.update()
        except Exception:
            pass


__all__ = ["CommandBar", "RibbonTab", "RibbonAction"]
