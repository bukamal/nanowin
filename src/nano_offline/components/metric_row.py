"""Shared, responsive KPI/metric strip for every dashboard-like screen.

A fixed three-per-row grid read fine on a phone but stretched into three
tall columns on a wide desktop window, leaving big empty gaps. This helper
puts the cards in a *single horizontal strip* once there is room, and wraps
to a two-up grid on phones -- one rule, applied everywhere a summary row is
shown, so items/finance/invoices/dashboard stop each inventing their own
column widths.
"""

from __future__ import annotations

import flet as ft


def metric_controls(cards: list[ft.Control], *, mobile_cols: int = 2) -> list[ft.Control]:
    """Apply the shared breakpoint columns to a list of metric cards.

    Cards are laid out ``mobile_cols``-across on phones and evenly wrapped
    from the medium breakpoint up, capped at four per row (each takes
    ``12 / per_row`` columns). The cap matters: squirrelling five or six
    cards onto a single line left each one too narrow for its money value,
    which wrapped the text and stretched the whole row vertically (the
    customer/supplier detail sheet, five KPIs, was the visible case).
    Balances the wrap instead of leaving one orphan card on the last line.
    """
    n = max(1, len(cards))
    rows = -(-n // 4)  # ceil(n / 4): at most four cards per row
    per_row = -(-n // rows)  # ceil(n / rows): balanced wrap
    md = max(1, 12 // per_row)
    xs = max(1, 12 // max(1, mobile_cols))
    out: list[ft.Control] = []
    for card in cards:
        control = card if isinstance(card, ft.Container) else ft.Container(card)
        control.col = {"xs": xs, "sm": xs, "md": md, "lg": md, "xl": md}
        out.append(control)
    return out


def metric_row(cards: list[ft.Control], *, mobile_cols: int = 2, spacing: int = 8) -> ft.ResponsiveRow:
    return ft.ResponsiveRow(metric_controls(cards, mobile_cols=mobile_cols), spacing=spacing, run_spacing=spacing)


__all__ = ["metric_row", "metric_controls"]
