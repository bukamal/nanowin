from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import flet as ft

from nano_offline.components import SegmentOption, SegmentedToggle
from nano_offline.core import barcode_settings
from nano_offline.core.theme import Colors

PrintAction = Callable[[str, int], Awaitable[None]]

#: Last confirmed profile, reused by prints that run without a dialog
#: (e.g. POS "print automatically after payment") so they never interrupt
#: the cashier with a paper-type prompt on every sale.
PRINT_LAYOUT_KEY = "print_layout"
PRINT_ROLL_WIDTH_KEY = "print_roll_width"
DEFAULT_PRINT_LAYOUT = "sheet"
DEFAULT_PRINT_ROLL_WIDTH = "58"


def saved_print_profile(settings) -> tuple[str, int]:
    """Return ``(layout, roll_width_mm)`` from the last confirmed choice."""
    layout = (settings.get(PRINT_LAYOUT_KEY, DEFAULT_PRINT_LAYOUT) or DEFAULT_PRINT_LAYOUT).strip()
    if layout not in {"sheet", "roll"}:
        layout = DEFAULT_PRINT_LAYOUT
    width_key = (settings.get(PRINT_ROLL_WIDTH_KEY, DEFAULT_PRINT_ROLL_WIDTH) or DEFAULT_PRINT_ROLL_WIDTH).strip()
    if width_key not in barcode_settings.LABEL_ROLL_WIDTH_MM:
        width_key = barcode_settings.DEFAULT_LABEL_ROLL_WIDTH
    return layout, barcode_settings.LABEL_ROLL_WIDTH_MM[width_key]


def open_print_profile_dialog(
    page: ft.Page,
    settings,
    on_confirm: PrintAction,
    *,
    title: str = "إعدادات الطباعة",
    initial_layout: str | None = None,
    initial_roll_width: str | None = None,
) -> None:
    """Ask for A4/thermal layout before handing a document to the OS printer."""
    saved_layout, saved_width = saved_print_profile(settings)
    layout = SegmentedToggle(
        options=[
            SegmentOption("sheet", "ورق A4", ft.Icons.DESCRIPTION_OUTLINED),
            SegmentOption("roll", "طابعة حرارية", ft.Icons.RECEIPT_LONG_OUTLINED),
        ],
        value=initial_layout if initial_layout in {"sheet", "roll"} else saved_layout,
    )
    width = SegmentedToggle(
        options=[SegmentOption(key, label) for key, label in barcode_settings.LABEL_ROLL_WIDTH_LABELS.items()],
        value=(
            initial_roll_width
            if initial_roll_width in barcode_settings.LABEL_ROLL_WIDTH_MM
            else str(saved_width)
        ),
    )
    width_row = ft.Row(
        [ft.Text("عرض اللفة", size=12, color=Colors.TEXT_SECONDARY), width],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        visible=layout.value == "roll",
    )
    hint = ft.Text(size=11, color=Colors.TEXT_SECONDARY)

    def refresh(*, update: bool = True) -> None:
        is_roll = layout.value == "roll"
        width_row.visible = is_roll
        hint.value = f"تخطيط مناسب للفة بعرض {width.value} مم." if is_roll else "تخطيط صفحة A4 للطباعة المكتبية."
        if update:
            page.update()

    layout.on_change = lambda _: refresh()
    width.on_change = lambda _: refresh()
    refresh(update=False)

    dialog = ft.AlertDialog(modal=True)

    async def confirm(_=None) -> None:
        selected_layout = layout.value or "sheet"
        width_key = width.value or barcode_settings.DEFAULT_LABEL_ROLL_WIDTH
        roll_width_mm = barcode_settings.LABEL_ROLL_WIDTH_MM.get(
            width_key,
            barcode_settings.LABEL_ROLL_WIDTH_MM[barcode_settings.DEFAULT_LABEL_ROLL_WIDTH],
        )
        try:
            settings.set(PRINT_LAYOUT_KEY, selected_layout)
            settings.set(PRINT_ROLL_WIDTH_KEY, width_key)
        except Exception:
            pass
        page.close(dialog)
        await asyncio.sleep(0.1)
        await on_confirm(selected_layout, roll_width_mm)

    dialog.title = ft.Text(title)
    dialog.content = ft.Column(
        [
            ft.Text("اختر تنسيق الورق قبل فتح نافذة الطباعة."),
            layout,
            width_row,
            hint,
        ],
        tight=True,
        spacing=12,
    )
    dialog.actions = [
        ft.TextButton("إلغاء", on_click=lambda _: page.close(dialog)),
        ft.FilledButton("متابعة للطباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=confirm),
    ]
    page.open(dialog)


def apply_print_profile(html: str, layout: str, roll_width_mm: int) -> str:
    """Override document page geometry for the selected paper profile."""
    if layout == "roll":
        style = f"""
<style id="nano-print-profile">
@page {{ size: {int(roll_width_mm)}mm auto; margin: 0; }}
html, body {{ width: {int(roll_width_mm)}mm !important; min-width: 0 !important; max-width: {int(roll_width_mm)}mm !important; margin: 0 !important; padding: 0 !important; }}
body {{ font-size: 9px !important; line-height: 1.4 !important; }}
.sheet {{ width: 100% !important; max-width: 100% !important; }}
.header {{ display: block !important; padding: 7px !important; margin-bottom: 8px !important; border-radius: 5px !important; }}
.logo {{ max-width: 42px !important; height: 32px !important; }}
.brand {{ font-size: 16px !important; }}
.doc-title {{ font-size: 13px !important; }}
.grid, .metrics {{ display: block !important; }}
.metric {{ margin: 4px 0 !important; padding: 6px !important; }}
table {{ width: 100% !important; table-layout: fixed !important; font-size: 8px !important; }}
th, td {{ padding: 3px 2px !important; overflow-wrap: anywhere !important; }}
</style>
"""
    else:
        style = """
<style id="nano-print-profile">
@page { size: A4; margin: 12mm; }
html, body { width: auto !important; min-width: 0 !important; max-width: 100% !important; margin: 0 !important; padding: 0 !important; }
.sheet { width: 100% !important; max-width: 100% !important; }
</style>
"""

    marker = "</head>"
    if marker in html.lower():
        head_end = html.lower().index(marker)
        return html[:head_end] + style + html[head_end:]
    html_end = html.lower().rfind("</html>")
    if html_end >= 0:
        return html[:html_end] + "<head>" + style + "</head>" + html[html_end:]
    return "<!doctype html><html><head>" + style + "</head><body>" + html + "</body></html>"
