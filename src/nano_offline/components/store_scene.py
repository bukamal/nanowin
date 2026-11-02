"""Smart-store home scene (software-rendered 2.5D).

A Windows-first "you are inside a smart store" home screen. Flet has no 3D
engine -- no WebGL, no perspective matrix -- so the depth is real but
software: we project an isometric diorama ourselves and draw the geometry on
:class:`flet.canvas.Canvas` (a 2D vector surface), then overlay *native* Flet
controls for every label and tap target. Text stays axis-aligned and Arabic
shaping/RTL is handled by Flutter, never by the canvas.

Stages folded in here:
  * A - static diorama: floor, six extruded zones, live KPI labels.
  * B - interaction: hover/tap lift, a per-zone quick-action ring, and a flat
        responsive fallback for narrow windows.
  * C - life: pulsing alert beacons, a "new sale" pulse from the cashier to
        the vault, all silenced by the reduce-motion setting.
  * D - atmosphere: time-of-day ambient lighting, brand-accent glow, and a
        command-palette orb.

Everything degrades: if the pinned Flet build lacks the canvas API,
``render_store_scene`` raises and the shell falls back to the flat board.

Coordinate model
----------------
World axes: ``gx`` (screen right-down), ``gy`` (screen left-down), ``z`` up.
Isometric projection with a half-height tile::

    sx = ox + (gx - gy) * tw / 2
    sy = oy + (gx + gy) * th / 2 - z          (th = tw / 2)

Painter's algorithm draws zones ordered by ``gx + gy`` ascending (far first).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

import flet as ft

from nano_offline.core.theme import Colors, Radius, Shadow, get_brand_accent, is_dark

try:  # pragma: no cover - exercised only under the pinned Flet build
    from flet.canvas import Canvas as FCanvas
    from flet.canvas import Circle as FCircle
    from flet.canvas import Line as FLine
    from flet.canvas import Oval as FOval
    from flet.canvas import Path as FPath
    from flet.core.painting import Paint, PaintingStyle

    _CANVAS_OK = True
except Exception:  # pragma: no cover
    FCanvas = None  # type: ignore
    FCircle = FLine = FOval = FPath = None  # type: ignore
    Paint = PaintingStyle = None  # type: ignore
    _CANVAS_OK = False

LAST_SALES_KEY = "home.scene.last_sales"
NARROW_WIDTH = 820.0


# --------------------------------------------------------------------------- #
# color helpers
# --------------------------------------------------------------------------- #
def _blend(hex_a: str, hex_b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    a = str(hex_a).lstrip("#")
    b = str(hex_b).lstrip("#")
    if len(a) != 6 or len(b) != 6:
        return hex_a
    try:
        av = [int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)]
        bv = [int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)]
    except ValueError:
        return hex_a
    out = [max(0, min(255, round(av[i] + (bv[i] - av[i]) * t))) for i in range(3)]
    return f"#{out[0]:02X}{out[1]:02X}{out[2]:02X}"


def _lighten(hex6: str, t: float) -> str:
    return _blend(hex6, "#FFFFFF", t)


def _darken(hex6: str, t: float) -> str:
    return _blend(hex6, "#000000", t)


def _alpha(hex6: str, a: int) -> str:
    """Return a Flet ``#AARRGGBB`` color from a ``#RRGGBB`` token + alpha."""
    a = max(0, min(255, int(a)))
    return f"#{a:02X}{str(hex6).lstrip('#')[:6]}"


def _ambient(hour: int, dark: bool) -> tuple[str, str]:
    """Time-of-day background gradient (top, bottom)."""
    if dark:
        if 5 <= hour < 17:
            return "#0B1426", "#101C33"
        return "#070B16", "#0E1730"
    if 5 <= hour < 11:
        return "#EAF3FF", "#D8E7FA"
    if 11 <= hour < 17:
        return "#F8FAFC", "#EBF1F9"
    if 17 <= hour < 21:
        return "#FFF3E6", "#FFE3C4"
    return "#EDF0FF", "#E0E5FF"


# --------------------------------------------------------------------------- #
# projection + model
# --------------------------------------------------------------------------- #
@dataclass
class Iso:
    ox: float
    oy: float
    tw: float

    @property
    def th(self) -> float:
        return self.tw * 0.5

    def proj(self, gx: float, gy: float, z: float = 0.0) -> tuple[float, float]:
        return (
            self.ox + (gx - gy) * self.tw * 0.5,
            self.oy + (gx + gy) * self.th * 0.5 - z,
        )


@dataclass
class ZoneAction:
    id: str
    label: str
    icon: str


@dataclass
class Zone:
    key: str
    title: str
    icon: str
    color: str
    gx: float
    gy: float
    w: float = 2.0
    d: float = 2.0
    h_units: float = 0.9
    metric_label: str = ""
    metric_value: str = ""
    alert: bool = False
    featured: bool = False
    route: str | None = None
    actions: list[ZoneAction] = field(default_factory=list)

    @property
    def depth(self) -> float:
        return self.gx + self.gy


FLOOR = (0.4, 0.4, 9.6, 7.6)  # gx0, gy0, gx1, gy1


def build_scene_model(
    ctx,
    *,
    fmt: Callable[[float], str],
    can: Callable[[str], bool] | None = None,
) -> list[Zone]:
    """Map live business KPIs onto the store zones.

    Kept deliberately free of Flet/geometry so it can be unit-tested with a
    fake ``ctx``; ``fmt`` is the shell's privacy-aware money formatter and
    ``can`` the current session's permission check.
    """

    def allowed(perm: str) -> bool:
        return True if can is None else bool(can(perm))

    summary: dict = {}
    today: dict = {}
    weekly: dict = {}
    last_close: dict | None = None
    try:
        summary = ctx.dashboard.summary()
    except Exception:
        summary = {}
    try:
        today = ctx.dashboard.today_summary()
    except Exception:
        today = {}
    try:
        weekly = ctx.dashboard.weekly_owner_summary()
    except Exception:
        weekly = {}
    try:
        last_close = ctx.cash_day_close.last_close()
    except Exception:
        last_close = None

    closed_today = bool(last_close and last_close.get("date") == date.today().isoformat())
    low_stock = int(weekly.get("low_stock_count") or 0)
    receivables = float(summary.get("receivables") or 0)

    zones: list[Zone] = []
    if allowed("invoices"):
        zones.append(
            Zone(
                "pos", "الكاشير ونقطة البيع", ft.Icons.POINT_OF_SALE_OUTLINED, Colors.PRIMARY,
                5, 6, w=2.4, d=2.2, h_units=1.3,
                metric_label="مبيعات اليوم", metric_value=fmt(today.get("total")), featured=True, route="pos",
                actions=[
                    ZoneAction("new_sale", "بيع سريع", ft.Icons.POINT_OF_SALE),
                    ZoneAction("last_sales", "آخر المبيعات", ft.Icons.RECEIPT_LONG_OUTLINED),
                ],
            )
        )
    if allowed("items"):
        zones.append(
            Zone(
                "items", "الأرفف والمخزون", ft.Icons.INVENTORY_2_OUTLINED, Colors.PURPLE,
                2, 2, metric_label="قيمة المخزون", metric_value=fmt(summary.get("inventory_value")),
                alert=low_stock > 0, route="items",
                actions=[ZoneAction("stocktake", "جرد سريع", ft.Icons.FACT_CHECK_OUTLINED)],
            )
        )
    if allowed("invoices"):
        zones.append(
            Zone(
                "purchase", "التوريد والاستلام", ft.Icons.LOCAL_SHIPPING_OUTLINED, Colors.ORANGE,
                8, 2, metric_label="المشتريات", metric_value=fmt(summary.get("purchases")), route="purchase",
                actions=[
                    ZoneAction("new_purchase", "فاتورة شراء", ft.Icons.ADD_SHOPPING_CART),
                    ZoneAction("open_suppliers", "الموردون", ft.Icons.LOCAL_SHIPPING_OUTLINED),
                ],
            )
        )
    if allowed("finance"):
        zones.append(
            Zone(
                "finance", "الصندوق والمالية", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, Colors.SUCCESS,
                2, 6, metric_label="رصيد الصندوق", metric_value=fmt(summary.get("cash")),
                alert=not closed_today, route="finance",
                actions=[
                    ZoneAction("receipt", "سند قبض", ft.Icons.PAYMENTS_OUTLINED),
                    ZoneAction("payment", "سند دفع", ft.Icons.REQUEST_QUOTE_OUTLINED),
                ],
            )
        )
    if allowed("customers"):
        zones.append(
            Zone(
                "customers", "العملاء والذمم", ft.Icons.PEOPLE_OUTLINE, Colors.WARNING,
                8, 6, metric_label="ذمم العملاء", metric_value=fmt(receivables),
                alert=receivables > 0, route="customers",
                actions=[
                    ZoneAction("statement", "كشف حساب", ft.Icons.DESCRIPTION_OUTLINED),
                    ZoneAction("receipt", "سند قبض", ft.Icons.PAYMENTS_OUTLINED),
                ],
            )
        )
    if allowed("reports"):
        zones.append(
            Zone(
                "reports", "مركز الرؤية", ft.Icons.QUERY_STATS_OUTLINED, Colors.PRIMARY_DARK,
                5, 2, metric_label="صافي الربح", metric_value=fmt(summary.get("net_profit")), route="reports",
            )
        )
    return zones


# --------------------------------------------------------------------------- #
# canvas geometry
# --------------------------------------------------------------------------- #
def _path(points: list[tuple[float, float]]):
    elts = [FPath.MoveTo(points[0][0], points[0][1])]
    elts += [FPath.LineTo(x, y) for x, y in points[1:]]
    elts.append(FPath.Close())
    return elts


def _fill(points: list[tuple[float, float]], color: str):
    return FPath(elements=_path(points), paint=Paint(color=color, style=PaintingStyle.FILL, anti_alias=True))


def _stroke(points: list[tuple[float, float]], color: str, width: float = 1.0):
    return FPath(
        elements=_path(points),
        paint=Paint(color=color, style=PaintingStyle.STROKE, stroke_width=width, anti_alias=True),
    )


def _floor_shapes(iso: Iso, *, dark: bool, accent: str) -> list:
    gx0, gy0, gx1, gy1 = FLOOR
    corners = [iso.proj(gx0, gy0), iso.proj(gx1, gy0), iso.proj(gx1, gy1), iso.proj(gx0, gy1)]
    slab = _blend("#121A2B" if dark else "#E6ECF5", accent, 0.06)
    line = "#33415C" if dark else "#CBD6E6"
    shapes = [_fill(corners, slab)]
    gx = gx0 + 1
    while gx < gx1:
        shapes.append(FLine(*iso.proj(gx, gy0), *iso.proj(gx, gy1), paint=Paint(color=_alpha(line, 140), stroke_width=1)))
        gx += 1
    gy = gy0 + 1
    while gy < gy1:
        shapes.append(FLine(*iso.proj(gx0, gy), *iso.proj(gx1, gy), paint=Paint(color=_alpha(line, 140), stroke_width=1)))
        gy += 1
    return shapes


def _zone_shapes(iso: Iso, z: Zone, *, dark: bool, accent: str, lift: float = 0.0, highlight: bool = False):
    x0, y0 = z.gx - z.w / 2, z.gy - z.d / 2
    x1, y1 = z.gx + z.w / 2, z.gy + z.d / 2
    h = max(14.0, (z.h_units + lift) * iso.tw)

    def p(x, y, zz=0.0):
        return iso.proj(x, y, zz)

    base = [p(x0, y0), p(x1, y0), p(x1, y1), p(x0, y1)]
    top = [p(x0, y0, h), p(x1, y0, h), p(x1, y1, h), p(x0, y1, h)]

    shapes: list = []
    cx, cy = p(z.gx, z.gy, 0.0)
    shadow_w = z.w * iso.tw
    shapes.append(
        FOval(
            cx - shadow_w * 0.5,
            cy - shadow_w * 0.16,
            width=shadow_w,
            height=shadow_w * 0.34,
            paint=Paint(color=_alpha("#000000", 100 if dark else 54)),
        )
    )
    if z.featured or highlight:
        glow = accent if z.featured else z.color
        glow_w = z.w * iso.tw * (1.55 if z.featured else 1.25)
        shapes.append(
            FOval(
                cx - glow_w * 0.5,
                cy - glow_w * 0.16,
                width=glow_w,
                height=glow_w * 0.38,
                paint=Paint(color=_alpha(glow, 80 if z.featured else 55)),
            )
        )

    east = [top[1], top[2], base[2], base[1]]   # +x face, catches the light
    south = [top[3], top[2], base[2], base[3]]  # +y face, in shadow
    shapes.append(_fill(east, _darken(z.color, 0.30)))
    shapes.append(_fill(south, _darken(z.color, 0.48)))
    shapes.append(_fill(top, _lighten(z.color, 0.16 if highlight else 0.10)))
    shapes.append(_stroke(top, _alpha("#FFFFFF", 120 if highlight else 60), 1.6 if highlight else 1.0))

    if z.alert:
        bx, by = p(z.gx, z.gy, h + iso.tw * 0.36)
        r = max(5.0, iso.tw * 0.11)
        shapes.append(FCircle(bx, by, r * 1.9, paint=Paint(color=_alpha(Colors.WARNING, 55))))
        shapes.append(FCircle(bx, by, r, paint=Paint(color=_alpha(Colors.WARNING, 235))))
    return shapes, p(z.gx, z.gy, h)


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
def render_store_scene(
    page: ft.Page,
    ctx,
    *,
    fmt: Callable[[float], str],
    on_select: Callable[[str], None],
    on_action: Callable[[str], None] | None = None,
    can: Callable[[str], bool] | None = None,
    reduce_motion: bool = False,
    open_command_palette: Callable[[], None] | None = None,
) -> ft.Control:
    """Build the interactive smart-store home surface.

    Raises ``RuntimeError`` when the pinned Flet build has no canvas API so
    the caller can fall back to the flat board.
    """
    if not _CANVAS_OK:
        raise RuntimeError("canvas API unavailable in this Flet build")

    on_action = on_action or (lambda _id: None)
    zones = build_scene_model(ctx, fmt=fmt, can=can)
    dark = is_dark()
    accent = get_brand_accent()

    try:
        width0 = float(page.width or 1200)
        height0 = float(page.height or 720)
    except Exception:
        width0, height0 = 1200.0, 720.0
    state = {"w": width0, "h": max(340.0, height0 - 120.0), "ring": None, "hover": None, "phase": False, "alive": True}

    if state["w"] < NARROW_WIDTH:
        return _flat_scene(zones, on_select=on_select, on_action=on_action, open_command_palette=open_command_palette, accent=accent, dark=dark)

    def fit(w: float, h: float) -> Iso:
        tw = max(34.0, min(w * 0.165, h * 0.145))
        ox = w * 0.5 - tw * 0.5
        oy = h * 0.52 - 9.0 * tw * 0.25
        return Iso(ox, oy, tw)

    canvas = FCanvas(shapes=[], expand=True, resize_interval=180)

    def label_zone_height(z: Zone, iso: Iso) -> float:
        return max(14.0, z.h_units * iso.tw)

    def _redraw_canvas() -> None:
        try:
            layout(state["w"], state["h"])
            canvas.update()
        except Exception:
            pass

    def make_hover(z: Zone):
        def _handler(e):
            hovered = str(getattr(e, "data", "")).lower() in {"true", "1"}
            state["hover"] = z.key if hovered else None
            label = labels.get(z.key)
            if label is not None:
                label.scale = 1.05 if hovered else 1.0
                try:
                    label.update()
                except Exception:
                    pass
            _redraw_canvas()
        return _handler

    # ---- native overlays -------------------------------------------------- #
    def build_label(z: Zone) -> ft.Container:
        badge = ft.Container(
            ft.Icon(z.icon, size=18, color=Colors.WHITE),
            width=34, height=34, alignment=ft.alignment.center,
            bgcolor=z.color, border_radius=10,
        )
        if z.alert:
            title_color = Colors.WARNING_DARKER
        elif z.featured:
            title_color = Colors.PRIMARY_DARK
        else:
            title_color = Colors.TEXT_PRIMARY
        return ft.Container(
            ft.Row(
                [
                    badge,
                    ft.Column(
                        [
                            ft.Text(z.title, size=12, weight=ft.FontWeight.BOLD, color=title_color, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(f"{z.metric_label}: {z.metric_value}", size=10, color=Colors.TEXT_SECONDARY, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=1, expand=True, tight=True,
                    ),
                    ft.Icon(ft.Icons.CHEVRON_LEFT, size=16, color=Colors.TEXT_FAINT),
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=7),
            bgcolor=_alpha(Colors.WHITE, 244),
            border=ft.border.all(2 if z.featured else 1, z.color if (z.featured or z.alert) else Colors.BORDER),
            border_radius=Radius.MD,
            shadow=Shadow.SM,
            ink=True,
            animate_scale=ft.Animation(140, ft.AnimationCurve.EASE_OUT),
            tooltip=f"{z.title} — {z.metric_label}",
            on_click=(lambda _e=None, _z=z: open_ring(_z)),
            on_hover=make_hover(z),
        )

    labels: dict[str, ft.Container] = {z.key: build_label(z) for z in zones}

    # animated alert beacons (native, so they can pulse without redrawing)
    beacons: dict[str, ft.Container] = {}
    for z in zones:
        if not z.alert:
            continue
        beacons[z.key] = ft.Container(
            width=12, height=12, border_radius=6,
            bgcolor=Colors.WARNING,
            border=ft.border.all(2, _alpha("#FFFFFF", 200)),
            scale=1.0, opacity=0.85,
            animate_scale=ft.Animation(850, ft.AnimationCurve.EASE_IN_OUT),
            animate_opacity=ft.Animation(850, ft.AnimationCurve.EASE_IN_OUT),
        )

    ring = ft.Container(visible=False, width=228, padding=8, bgcolor=_alpha(Colors.WHITE, 250),
                        border=ft.border.all(1, Colors.BORDER), border_radius=Radius.LG, shadow=Shadow.LG)

    def ring_actions(z: Zone) -> ft.Control:
        rows: list[ft.Control] = [
            ft.Text(z.title, size=12, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
            ft.Text(f"{z.metric_label}: {z.metric_value}", size=10, color=Colors.TEXT_SECONDARY),
            ft.Divider(height=1, color=Colors.BORDER_ALT),
        ]
        if z.route:
            rows.append(
                ft.FilledButton("فتح القسم", icon=ft.Icons.OPEN_IN_NEW_ROUNDED, width=9999,
                                on_click=lambda _e=None, _key=z.key: _do_select(_key))
            )
        for act in z.actions:
            rows.append(
                ft.TextButton(
                    act.label, icon=act.icon, width=9999,
                    on_click=lambda _e=None, _id=act.id: _do_action(_id),
                )
            )
        rows.append(ft.TextButton("إغلاق", icon=ft.Icons.CLOSE_ROUNDED, width=9999, on_click=lambda _e=None: close_ring()))
        return ft.Column(rows, spacing=4, tight=True)

    def open_ring(z: Zone) -> None:
        state["ring"] = z.key
        ring.content = ring_actions(z)
        _place_ring(z)
        ring.visible = True
        try:
            ring.update()
        except Exception:
            pass

    def close_ring() -> None:
        state["ring"] = None
        ring.visible = False
        try:
            ring.update()
        except Exception:
            pass

    def _do_select(key: str) -> None:
        close_ring()
        on_select(key)

    def _do_action(action_id: str) -> None:
        close_ring()
        on_action(action_id)

    def _place_ring(z: Zone, iso: Iso | None = None) -> None:
        iso = iso or fit(state["w"], state["h"])
        sx, sy = iso.proj(z.gx, z.gy, label_zone_height(z, iso))
        ring.left = min(max(8.0, sx - 114.0), max(8.0, state["w"] - 236.0))
        ring.top = min(max(8.0, sy + 24.0), max(8.0, state["h"] - 220.0))

    header = ft.Container(
        ft.Row(
            [
                ft.Container(width=12, height=12, border_radius=6, bgcolor=accent),
                ft.Column(
                    [
                        ft.Text("متجرك الذكي", size=20, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                        ft.Text("اختر منطقة من المشهد — أو اضغط للفتح والإجراءات السريعة", size=11, color=Colors.TEXT_SECONDARY),
                    ],
                    spacing=2, tight=True,
                ),
            ],
            spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        ),
        left=20, top=14,
    )
    hint = ft.Container(
        ft.Text("مسح حيّ لبيانات نشاطك — Ctrl+K للأوامر.", size=10, color=Colors.TEXT_FAINT),
        right=20, bottom=12,
    )
    orb = ft.Container(
        ft.Icon(ft.Icons.KEYBOARD_COMMAND_KEY_ROUNDED, size=24, color=Colors.WHITE),
        width=56, height=56, border_radius=28, bgcolor=accent, alignment=ft.alignment.center,
        shadow=Shadow.LG, ink=True, tooltip="لوحة الأوامر (Ctrl+K)",
        on_click=lambda _e=None: (open_command_palette() if open_command_palette else None),
    )

    top, bottom = _ambient(datetime.now().hour, dark)
    bg = ft.Container(
        expand=True,
        gradient=ft.LinearGradient(colors=[top, bottom], begin=ft.alignment.top_center, end=ft.alignment.bottom_center),
    )

    stack = ft.Stack([bg, canvas, *beacons.values(), *labels.values(), ring, header, hint, orb], expand=True)

    # ---- command-center pulse + ambient beacon animation ------------------ #
    def spawn_pulse(src: Zone | None, dst: Zone | None, color: str) -> None:
        if reduce_motion or src is None or dst is None:
            return
        iso = fit(state["w"], state["h"])
        x0, y0 = iso.proj(src.gx, src.gy, label_zone_height(src, iso))
        x1, y1 = iso.proj(dst.gx, dst.gy, label_zone_height(dst, iso))
        dot = ft.Container(
            width=18, height=18, border_radius=9, bgcolor=color, shadow=Shadow.MD,
            left=x0 - 9, top=y0 - 9, opacity=1.0,
            animate_position=ft.Animation(650, ft.AnimationCurve.EASE_IN_OUT),
            animate_opacity=ft.Animation(300, ft.AnimationCurve.EASE_IN_OUT),
        )
        stack.controls.append(dot)

        async def _run() -> None:
            await asyncio.sleep(0.06)
            try:
                dot.left = x1 - 9
                dot.top = y1 - 9
                dot.opacity = 0.15
                page.update()
            except Exception:
                return
            await asyncio.sleep(0.75)
            try:
                stack.controls.remove(dot)
                page.update()
            except Exception:
                pass

        try:
            page.run_task(_run)
        except Exception:
            pass

    async def _ambient_loop() -> None:
        while state["alive"]:
            await asyncio.sleep(0.85)
            if not state["alive"] or not beacons:
                return
            if getattr(stack, "page", None) is None:
                return
            state["phase"] = not state["phase"]
            for b in beacons.values():
                b.scale = 1.7 if state["phase"] else 1.0
                b.opacity = 0.4 if state["phase"] else 0.85
            try:
                page.update()
            except Exception:
                return

    if beacons and not reduce_motion:
        try:
            page.run_task(_ambient_loop)
        except Exception:
            pass

    # "New sale since you were last here" -> cashier-to-vault pulse.
    try:
        today_total = float(ctx.dashboard.today_summary().get("total") or 0)
    except Exception:
        today_total = 0.0
    try:
        prev_raw = ctx.settings.get(LAST_SALES_KEY)
    except Exception:
        prev_raw = None
    if prev_raw is None:
        try:
            ctx.settings.set(LAST_SALES_KEY, str(today_total))
        except Exception:
            pass
    else:
        try:
            if today_total > float(prev_raw) + 1e-6:
                by_key = {z.key: z for z in zones}
                spawn_pulse(by_key.get("pos"), by_key.get("finance"), accent)
        except Exception:
            pass
        try:
            ctx.settings.set(LAST_SALES_KEY, str(today_total))
        except Exception:
            pass

    def layout(width: float, height: float) -> None:
        width = max(320.0, float(width))
        height = max(340.0, float(height))
        iso = fit(width, height)
        shapes = _floor_shapes(iso, dark=dark, accent=accent)
        for z in sorted(zones, key=lambda z: z.depth):
            highlight = z.key in {state["hover"], state["ring"]}
            lift = 0.22 if highlight else 0.0
            z_shapes, _ = _zone_shapes(iso, z, dark=dark, accent=accent, lift=lift, highlight=highlight)
            shapes.extend(z_shapes)
        canvas.shapes = shapes
        for z in zones:
            label = labels[z.key]
            w_px = max(150.0, iso.tw * z.w * 0.92)
            sx, sy = iso.proj(z.gx, z.gy, max(14.0, z.h_units * iso.tw))
            label.width = w_px
            label.left = sx - w_px / 2
            label.top = sy - 22
        for z in zones:
            beacon = beacons.get(z.key)
            if beacon is None:
                continue
            sx, sy = iso.proj(z.gx, z.gy, label_zone_height(z, iso) + iso.tw * 0.36)
            beacon.left = sx - 6
            beacon.top = sy - 6
        if state["ring"] is not None:
            z = next((z for z in zones if z.key == state["ring"]), None)
            if z is not None:
                _place_ring(z, iso)
        orb.left = width / 2 - 28

    def on_resize(e) -> None:
        try:
            new_w = float(e.width or state["w"])
            new_h = float(e.height or state["h"])
        except Exception:
            return
        if abs(new_w - state["w"]) < 2 and abs(new_h - state["h"]) < 2:
            return
        state["w"], state["h"] = new_w, new_h
        layout(state["w"], state["h"])
        try:
            page.update()
        except Exception:
            pass

    canvas.on_resize = on_resize
    layout(state["w"], state["h"])
    # orb vertical anchor
    orb.bottom = 18
    return stack


# --------------------------------------------------------------------------- #
# flat fallback for narrow windows
# --------------------------------------------------------------------------- #
def _flat_scene(
    zones: list[Zone],
    *,
    on_select: Callable[[str], None],
    on_action: Callable[[str], None],
    open_command_palette: Callable[[], None] | None,
    accent: str,
    dark: bool,
) -> ft.Control:
    cards: list[ft.Control] = []
    for z in zones:
        actions_row: list[ft.Control] = [
            ft.FilledButton("فتح", icon=ft.Icons.OPEN_IN_NEW_ROUNDED, on_click=lambda _e=None, k=z.key: on_select(k)),
        ]
        for act in z.actions[:2]:
            actions_row.append(
                ft.OutlinedButton(act.label, icon=act.icon, on_click=lambda _e=None, i=act.id: on_action(i))
            )
        cards.append(
            ft.Container(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(ft.Icon(z.icon, size=20, color=Colors.WHITE), width=40, height=40,
                                             alignment=ft.alignment.center, bgcolor=z.color, border_radius=12),
                                ft.Column(
                                    [
                                        ft.Text(z.title, size=13, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                                        ft.Text(f"{z.metric_label}: {z.metric_value}", size=11,
                                                color=Colors.WARNING_DARKER if z.alert else Colors.TEXT_SECONDARY),
                                    ],
                                    spacing=2, expand=True, tight=True,
                                ),
                            ],
                            spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Row(actions_row, spacing=8, wrap=True),
                    ],
                    spacing=10,
                ),
                padding=14, bgcolor=Colors.WHITE,
                border=ft.border.all(2 if z.featured else 1, z.color if (z.featured or z.alert) else Colors.BORDER),
                border_radius=Radius.LG, shadow=Shadow.SM,
                col={"xs": 12, "sm": 6, "lg": 4},
            )
        )
    header = ft.Row(
        [
            ft.Container(width=12, height=12, border_radius=6, bgcolor=accent),
            ft.Text("متجرك الذكي", size=18, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
            ft.Container(expand=True),
            ft.TextButton("لوحة الأوامر", icon=ft.Icons.KEYBOARD_COMMAND_KEY_ROUNDED,
                          on_click=lambda _e=None: (open_command_palette() if open_command_palette else None)),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    body: list[ft.Control] = [header, ft.ResponsiveRow(cards, spacing=12, run_spacing=12, rtl=True)]
    return ft.Column(body, spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)


__all__ = ["Zone", "ZoneAction", "Iso", "build_scene_model", "render_store_scene"]
