"""Consistent 'load more' control for the list screens.

Every table (materials, invoices, customers, suppliers) used to grow its own
render window with a button whose label showed the *remaining* count. That
number decremented on every tap and read like a total, which confused users and
needed many taps. This control keeps the label stable («عرض المزيد») and shows
a clear "shown of total" caption underneath instead.
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from nano_offline.core.theme import Colors


def load_more_control(*, shown: int, total: int, on_click: Callable[[object], None]) -> ft.Control:
    return ft.Column(
        [
            ft.OutlinedButton(
                "عرض المزيد",
                icon=ft.Icons.EXPAND_MORE_ROUNDED,
                on_click=on_click,
            ),
            ft.Text(
                f"عرض {int(shown):,} من {int(total):,}",
                size=10,
                color=Colors.TEXT_FAINT,
            ),
        ],
        spacing=4,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )


__all__ = ["load_more_control"]
