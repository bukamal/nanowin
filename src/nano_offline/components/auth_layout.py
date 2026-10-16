"""Responsive scaffold shared by the pre-login gates (activation + login).

On a phone both gates stay a single centred card. On a wide/desktop window
that vertical stack looked stretched and empty, so it switches to a
horizontal split: a gradient brand panel beside the form. Using one scaffold
for both gates keeps them visually identical, as a single continuous
first-run experience.
"""

from __future__ import annotations

import flet as ft

from nano_offline.core.theme import Colors
from nano_offline.version import APP_VERSION

AUTH_BREAKPOINT = 900

_FEATURES = [
    (ft.Icons.WIFI_OFF_ROUNDED, "يعمل بدون إنترنت", "محاسبة ومخزون محلي بالكامل"),
    (ft.Icons.RECEIPT_LONG_OUTLINED, "فواتير وذمم وصندوق", "تدفق عمل محاسبي متكامل"),
    (ft.Icons.VERIFIED_USER_ROUNDED, "بياناتك على جهازك", "نسخ احتياطي مشفّر واستعادة آمنة"),
]


def _white(alpha: float):
    return ft.Colors.with_opacity(alpha, ft.Colors.WHITE)


def _brand_panel() -> ft.Container:
    return ft.Container(
        ft.Column(
            [
                ft.Container(
                    ft.Container(
                        ft.Image(src="icon.png", width=56, height=56, fit=ft.ImageFit.CONTAIN),
                        width=84, height=84, alignment=ft.alignment.center,
                        bgcolor=ft.Colors.WHITE, border_radius=24,
                    ),
                    padding=7, border_radius=28, bgcolor=_white(0.16),
                ),
                ft.Text("Nano | نانو", size=34, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Text("منظومة محاسبة ومخزون عربية تعمل محليًا", size=13, color=_white(0.9)),
                ft.Container(height=18),
                ft.Column([_feature_row(*f) for f in _FEATURES], spacing=14),
                ft.Container(expand=True),
                ft.Text(f"الإصدار {APP_VERSION}", size=11, color=_white(0.7)),
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        expand=True,
        padding=48,
        alignment=ft.alignment.center,
        gradient=ft.LinearGradient(
            colors=[Colors.PRIMARY, Colors.PRIMARY_DARK, Colors.PURPLE],
            begin=ft.alignment.top_left,
            end=ft.alignment.bottom_right,
        ),
    )


def _feature_row(icon, title: str, subtitle: str) -> ft.Row:
    return ft.Row(
        [
            ft.Container(
                ft.Icon(icon, color=ft.Colors.WHITE, size=18),
                width=40, height=40, alignment=ft.alignment.center,
                bgcolor=_white(0.18), border_radius=12,
            ),
            ft.Column(
                [
                    ft.Text(title, size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                    ft.Text(subtitle, size=11, color=_white(0.85)),
                ],
                spacing=1, expand=True,
            ),
        ],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def auth_scaffold(page: ft.Page, card: ft.Control) -> ft.Control:
    """Wrap an auth ``card`` in the phone (stacked) or desktop (split) shell."""
    wide = bool(page.width and page.width >= AUTH_BREAKPOINT)
    if wide:
        form_pane = ft.Container(
            ft.Column([card], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            expand=True,
            alignment=ft.alignment.center,
            padding=32,
            bgcolor=Colors.BACKGROUND,
        )
        return ft.Row(
            [_brand_panel(), form_pane],
            spacing=0,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )
    return ft.Container(
        ft.Column([card], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        expand=True,
        padding=16,
        alignment=ft.alignment.center,
        gradient=ft.LinearGradient(
            colors=[Colors.PRIMARY_BG, Colors.BACKGROUND, Colors.BACKGROUND],
            begin=ft.alignment.top_center,
            end=ft.alignment.bottom_center,
        ),
    )


__all__ = ["auth_scaffold", "AUTH_BREAKPOINT"]
