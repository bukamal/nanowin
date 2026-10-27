from __future__ import annotations

import asyncio
import math
import time

import copy
import flet as ft

from nano_offline.core.toast import toast

from nano_offline.components import SearchSelect, SelectAllTextField, SegmentedToggle, SegmentOption, empty_state
from nano_offline.components.form_sheet import render_form_sheet
from nano_offline.components.print_profile import (
    apply_print_profile,
    open_print_profile_dialog,
    save_print_layout,
    saved_print_profile,
)
from nano_offline.components.buttons import hero_button, stepper_icon_button
from nano_offline.services.invoice_service import InvoiceLineInput
from nano_offline.core.theme import Colors, IconSize, LazyPalette, Radius, Shadow
from nano_offline.core import currency
from nano_offline.core.margin_guard import cart_margin_warnings, check_sale_margin
from nano_offline.core import barcode_settings
from nano_offline.core import pos_settings
from nano_offline.core.units import is_weighted_unit
from nano_offline.core.platform_capabilities import supports
from nano_offline.core import sound as sound_engine
from nano_offline.core.voice_nlu import normalize_ar

# A small fixed palette so category chips get a stable, distinguishable
# color per category without needing a "color" column on the categories
# table. Cycled by index, not hashed, so the same category keeps the same
# color across a session (and across screens) as long as list order is
# stable — which it is, since list_categories() orders by name.
CHIP_PALETTE = LazyPalette("PRIMARY", "PURPLE_LIGHT", "ORANGE", "WARNING_DARK", "SUCCESS_ALT")

# Phase C: heuristic for "sold by weight" items -- there is no dedicated
# flag on units/items for this, so a cart line is treated as weighted when
# its base unit's name/abbreviation matches a common weight/volume unit.
# The shared list/heuristic now lives in core/units.py so the LAN price
# server (and any other surface) applies the exact same rule.

# Comfortable receipt-rail width on desktop: narrow enough to leave the
# catalog most of the screen, wide enough for name + qty stepper + line total.
CART_RAIL_WIDTH = 420


def _cart_key(item: dict, unit_id: int | None):
    """Stable cart identity for one item *and* selling unit.

    A carton barcode and the piece barcode of the same item are genuinely
    different lines (different price per unit, different stock factor), so
    they must not collapse into one row. Base-unit lines keep the plain
    ``item_id`` key -- that is what every existing caller/test assumes.
    """
    item_id = int(item["id"])
    if unit_id is None:
        return item_id
    base_unit_id = item.get("base_unit_id")
    if base_unit_id is not None and int(unit_id) == int(base_unit_id):
        return item_id
    return (item_id, int(unit_id))


def _cart_line_price(row: dict) -> float:
    """Stored-USD price for one *selected unit* of a cart line.

    ``unit_price`` is always per the row's own unit (piece or carton), so
    the line total is simply ``qty × price``. When no explicit override was
    recorded, the base unit falls back to the item's selling price; any
    other unit has already been stamped with its own price at add time.
    """
    price = row.get("unit_price")
    if price is not None:
        return float(price)
    item = row.get("item") or {}
    factor = float(row.get("factor") or 1)
    return float(item.get("selling_price") or 0) * factor


class POSCenter:
    """Fast, touch-first counter-sale screen.

    Separate from InvoiceCenter's full editor (customer/date/reference/notes)
    on purpose: POS optimizes for "scan or tap, then pay" with everything
    else defaulted. It still creates a normal ``sale`` invoice through
    ``InvoiceService`` so it stays fully compatible with reporting,
    inventory and party balances — there is no separate POS data model.
    """

    def __init__(
        self,
        page: ft.Page,
        ctx,
        content: ft.Container,
        native_files=None,
        on_title_change=None,
        on_saved=None,
        on_create_item=None,
        on_fullscreen_enter=None,
        on_fullscreen_exit=None,
    ):
        self.page = page
        self.ctx = ctx
        self.content = content
        self.native_files = native_files
        self.on_title_change = on_title_change
        self.on_saved = on_saved
        self.on_create_item = on_create_item
        # POS is the one screen that goes edge-to-edge (see show_center()):
        # these hide the app's own top header + sidebar/tab bar chrome for
        # the duration of the sale and restore it on exit -- optional so
        # POSCenter still works standalone (e.g. in tests) without a shell.
        self.on_fullscreen_enter = on_fullscreen_enter
        self.on_fullscreen_exit = on_fullscreen_exit

        # Cart lines keyed by item_id -> {"item": row, "qty": float}.
        # A dict (not a list) means "scan again" is a single lookup+bump,
        # never a duplicate row.
        self.cart: dict[int, dict] = {}
        self.cart_order: list[int] = []  # insertion order, for undo + display
        self.held: list[dict] = []  # [{"label": str, "cart": dict, "order": list, "customer_id": int|None}]
        self.customer_id: int | None = None
        self.received_digits: str = ""
        self.auto_print: bool = False
        # Session-only "last added" recall -- most-recent-first, deduped,
        # capped short on purpose (a quick-recall strip, not a history log).
        self.recent_item_ids: list[int] = []
        # Phase C: same idea for customers -- last few used *for a
        # completed sale* in this session, offered as quick-pick chips
        # under the customer field so a repeat customer's next sale
        # doesn't need a full dropdown search.
        self.recent_customer_ids: list[int] = []
        # Phase B: floating last-scan card + success overlay state
        self._last_scan_token: int = 0
        self._success_visible: bool = False
        # Price-check mode (F7): scans show the price card instead of
        # adding to the cart -- the "how much is this?" question a cashier
        # gets a dozen times a day.
        self._price_check_mode: bool = False
        # One-step undo for destructive cart actions (remove/clear). Holds
        # a deep copy of the cart before the action; F8 restores it.
        self._undo_state: dict | None = None
        self._esc_armed_at: float = 0.0
        # Which currency the cashier is typing cash in. ``None`` follows the
        # app's display currency; ``"USD"``/``"SYP"`` pin the other one so a
        # shop that prices in SYP can still take dollars without leaving POS.
        self._receive_currency: str | None = None

    def money(self, value: float) -> str:
        return currency.format_amount(value, self.ctx.settings)

    def _receive_settings_obj(self):
        """Settings view whose display currency is the cash-entry currency."""
        if self._receive_currency is None:
            return self.ctx.settings
        settings = dict(self.ctx.settings.get_all())
        settings[currency.DISPLAY_CURRENCY_KEY] = self._receive_currency
        return settings

    def _money_receive(self, value_usd: float) -> str:
        settings = self._receive_settings_obj()
        return currency.format_display_value(
            currency.to_display(value_usd, currency.get_effective_rate(settings)),
            settings,
        )

    @staticmethod
    def _qty(value) -> str:
        """Plain (non-currency) number formatting -- quantities.

        No thousands separators and no forced decimals, so quantities never
        render with commas.
        """
        v = float(value or 0)
        return str(int(v)) if v == int(v) else f"{v:.3f}".rstrip("0").rstrip(".")

    def notify(self, text: str, kind: str | None = None, sound_kind: str | None = None) -> None:
        toast(self.page, text, kind=kind, sound_kind=sound_kind)

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def show_center(self) -> None:
        if self.on_title_change:
            self.on_title_change("نقطة البيع", "بيع سريع بالكاونتر — مسح أو لمس ثم دفع")
        if self.on_fullscreen_enter:
            self.on_fullscreen_enter()
        try:
            self._build()
        except Exception as exc:
            self._show_center_error(exc)

    def _exit_pos(self, _=None) -> None:
        """Leave fullscreen POS. Falls back to a no-op if the shell didn't
        wire an exit target (on_fullscreen_exit already restores chrome;
        on_saved, if provided, is what actually navigates elsewhere)."""
        if self.on_fullscreen_exit:
            self.on_fullscreen_exit()
        try:
            self._pos_active = False
            if hasattr(self, "_pos_focus_state"):
                self._pos_focus_state["modal_open"] = False
            if hasattr(self, "_prev_on_keyboard"):
                self.page.on_keyboard_event = self._prev_on_keyboard
        except Exception:
            pass
        try:
            if getattr(self.ctx, "_pos_apply_voice", None) == self._apply_voice_command:
                self.ctx._pos_apply_voice = getattr(self, "_prev_voice_apply", None)
            if getattr(self.ctx, "_pos_cart_snapshot", None) == self._cart_snapshot:
                self.ctx._pos_cart_snapshot = getattr(self, "_prev_voice_snapshot", None)
        except Exception:
            pass

    def _pos_handle_resize(self) -> None:
        """No-op on desktop: the POS rail layout is width-independent.

        Kept because the shell's ``adapt_navigation`` still calls it through
        ``page._nano_pos_relayout`` while POS is fullscreen (it delegates
        resize events there instead of restoring chrome). A window resize no
        longer changes the sale layout, so there is nothing to rebuild.
        """
        return

    def _show_center_error(self, exc: Exception) -> None:
        if self.on_title_change:
            self.on_title_change("نقطة البيع", "تعذر تحميل شاشة نقطة البيع")
        message = str(exc).strip() or exc.__class__.__name__
        self.content.content = ft.Column(
            [
                ft.Icon(ft.Icons.ERROR_OUTLINE, size=42, color=Colors.DANGER_DARKER),
                ft.Text("تعذر تحميل شاشة نقطة البيع", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(message, size=11, color=Colors.DANGER_DARKER, selectable=True),
                ft.FilledButton("إعادة المحاولة", icon=ft.Icons.REFRESH, on_click=lambda _: self.show_center()),
            ],
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.page.update()

    # ------------------------------------------------------------------ #
    # Build
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        self._pos_active = True
        items = self.ctx.items.pos_catalog()
        self.item_map = {int(i["id"]): i for i in items}
        categories = self.ctx.definitions.list_categories()
        customers = self.ctx.customers.list()
        customer_map = {int(c["id"]): c for c in customers}

        state = {"query": "", "category_id": None}
        focus_state = {"modal_open": False}

        # ---- today's running total ---------------------------------------
        # A quick "how's today going" glance from inside the sale screen
        # itself -- no need to leave POS and open the full dashboard mid-shift.
        # Lives in the compact fullscreen header now (see pos_header below),
        # not stacked as its own row above the catalog.
        today_summary_text = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)

        def refresh_today_summary():
            try:
                s = self.ctx.dashboard.today_summary()
            except Exception:
                today_summary_text.value = ""
                return
            today_summary_text.value = f"مبيعات اليوم: {s['count']} فاتورة · {self.money(s['total'])}"

        refresh_today_summary()

        # ---- recent items (session recall) ---------------------------------
        recent_state = {"expanded": False}
        recent_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO, wrap=False, visible=False)
        recent_toggle_icon = ft.Icon(ft.Icons.EXPAND_MORE, size=16, color=Colors.TEXT_SECONDARY)
        recent_toggle_label = ft.Text("أضيفت مؤخرًا", size=11, color=Colors.TEXT_SECONDARY)
        recent_toggle = ft.Container(
            ft.Row([ft.Icon(ft.Icons.HISTORY, size=15, color=Colors.TEXT_SECONDARY), recent_toggle_label, recent_toggle_icon], spacing=5, tight=True),
            padding=ft.padding.symmetric(horizontal=8, vertical=5),
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.SM,
            ink=True,
            visible=False,
        )

        def toggle_recent(_=None):
            recent_state["expanded"] = not recent_state["expanded"]
            render_recent()
            self.page.update()

        recent_toggle.on_click = toggle_recent

        def render_recent():
            controls = []
            for item_id in self.recent_item_ids:
                item = self.item_map.get(item_id)
                if item is None:
                    continue
                controls.append(
                    ft.Container(
                        ft.Row(
                            [ft.Icon(ft.Icons.HISTORY, size=13, color=Colors.PRIMARY_DARK),
                             ft.Text(str(item["name"]), size=11, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)],
                            spacing=4, tight=True,
                        ),
                        padding=ft.padding.symmetric(horizontal=10, vertical=6),
                        bgcolor=Colors.WHITE,
                        border=ft.border.all(1, Colors.BORDER),
                        border_radius=16,
                        on_click=lambda _, i=int(item["id"]): self._add_item(i),
                        ink=True,
                    )
                )
            recent_row.controls = controls
            has_recent = bool(controls)
            recent_toggle.visible = has_recent
            recent_row.visible = has_recent and recent_state["expanded"]
            recent_toggle_icon.name = ft.Icons.EXPAND_LESS if recent_state["expanded"] else ft.Icons.EXPAND_MORE
            recent_toggle_label.value = f"أضيفت مؤخرًا · {len(controls)}"

        render_recent()

        # ---- search / barcode bar -------------------------------------
        search_field = SelectAllTextField(
            label="بحث عن مادة",
            hint_text="اكتب اسم المادة...",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            expand=True,
        )
        barcode_field = SelectAllTextField(
            label="مسح الباركود",
            hint_text="وجّه القارئ هنا واضغط Enter تلقائيًا",
            prefix_icon=ft.Icons.QR_CODE_2,
            dense=True,
            expand=True,
            bgcolor=Colors.WHITE,
            border_color=Colors.PRIMARY_BORDER,
            focused_border_color=Colors.PRIMARY,
            # keyboard_type=NONE keeps this field focusable -- still
            # needed for the hardware/USB "wedge" scanner to type into
            # and for Enter to trigger barcode_submit() -- without ever
            # raising the on-screen keyboard, since typing here by hand
            # is never the point of this field.
            keyboard_type=ft.KeyboardType.NONE,
        )

        async def _focus_barcode_after_mount() -> None:
            # NOT autofocus=True above: requesting focus that early races
            # Flutter's text-input channel attaching, so the field reports
            # hasFocus=True without the channel actually being attached --
            # and because it already "has focus" as far as Flutter is
            # concerned, a real tap on it afterwards doesn't trigger a
            # focus change either, so it's left in a half-focused state
            # until the user taps a different field first. A short yield
            # after the view has finished mounting lets the channel attach,
            # so the same focus() call actually takes, giving the
            # hardware/USB "wedge" scanner a genuinely focused field to
            # type into (and Enter to submit) without the user needing to
            # tap first -- keyboard_type=NONE on the field itself is what
            # keeps this from ever popping the on-screen keyboard.
            await asyncio.sleep(0.2)
            if self._pos_active and not focus_state["modal_open"]:
                barcode_field.focus()
                self.page.update()

        self.page.run_task(_focus_barcode_after_mount)

        def _resume_barcode_focus(_=None) -> None:
            focus_state["modal_open"] = False
            self.page.run_task(_focus_barcode_after_mount)

        def _mark_overlay_open() -> None:
            focus_state["modal_open"] = True

        self._pos_focus_state = focus_state
        self._pos_resume_barcode_focus = _resume_barcode_focus
        self._pos_mark_overlay_open = _mark_overlay_open

        def scan_button_handler(_):
            self.page.run_task(self._scan_with_camera)

        # ---- vertical category rail --------------------------------------
        # Categories are a stacked vertical list (not a horizontal chip
        # strip) so the whole taxonomy stays visible at a glance and a long
        # product grid never drags the categories out of reach.
        cat_list = ft.Column(spacing=4)
        best_list = ft.Column(spacing=4)
        best_state = {"expanded": False}

        def set_category(cat_id: int | None):
            state["category_id"] = cat_id
            render_categories()
            render_grid()
            self.page.update()

        def render_categories():
            controls = [
                self._rail_category(
                    "الكل", state["category_id"] is None,
                    Colors.TEXT_MUTED_DARK, ft.Icons.APPS, lambda _: set_category(None),
                )
            ]
            for idx, cat in enumerate(categories):
                active = state["category_id"] == int(cat["id"])
                color = CHIP_PALETTE[idx % len(CHIP_PALETTE)]
                controls.append(
                    self._rail_category(
                        cat["name"], active, color, ft.Icons.LABEL_OUTLINE,
                        lambda _, c=int(cat["id"]): set_category(c),
                    )
                )
            cat_list.controls = controls

        # "Best sellers" live in the rail as a vertical quick-add list rather
        # than an inline labelled section inside the grid. pos_catalog()
        # already returns best-sellers-first; keep only real sellers and cap
        # the strip short so it stays a quick pick, not a second catalog.
        best_sellers = sorted(
            (it for it in items if float(it.get("sold_qty") or 0) > 0),
            key=lambda it: float(it.get("sold_qty") or 0),
            reverse=True,
        )[:8]

        best_toggle_icon = ft.Icon(ft.Icons.EXPAND_MORE, size=16, color=Colors.TEXT_SECONDARY)
        best_toggle_label = ft.Text("", size=11, color=Colors.TEXT_SECONDARY)
        best_toggle = ft.Container(
            ft.Row([ft.Icon(ft.Icons.STAR_OUTLINE, size=15, color=Colors.TEXT_SECONDARY), best_toggle_label, best_toggle_icon], spacing=5, tight=True),
            padding=ft.padding.symmetric(horizontal=8, vertical=5),
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=Radius.SM,
            ink=True,
            visible=False,
        )

        def toggle_bestsellers(_=None):
            best_state["expanded"] = not best_state["expanded"]
            render_bestsellers()
            self.page.update()

        best_toggle.on_click = toggle_bestsellers

        def render_bestsellers():
            best_list.controls = [self._rail_product(it) for it in best_sellers]
            has_sellers = bool(best_sellers)
            best_toggle.visible = has_sellers
            best_list.visible = has_sellers and best_state["expanded"]
            best_toggle_label.value = f"الأكثر مبيعًا · {len(best_sellers)}"
            best_toggle_icon.name = ft.Icons.EXPAND_LESS if best_state["expanded"] else ft.Icons.EXPAND_MORE

        # ---- item grid ------------------------------------------------
        # ResponsiveRow (not GridView) so tiles reflow by column count and
        # size naturally within a scrolling page — no bounded-height parent
        # required, unlike GridView(expand=True).
        grid = ft.ResponsiveRow(spacing=10, run_spacing=10)

        def render_grid():
            raw_q = state["query"].strip()
            q = normalize_ar(raw_q.casefold())
            tokens = [t for t in q.split() if t]
            cat_id = state["category_id"]
            matched = []
            for item in items:
                if cat_id is not None and item.get("category_id") != cat_id:
                    continue
                if q:
                    name = normalize_ar(str(item["name"]).casefold())
                    barcode = str(item.get("barcode") or "")
                    if not (
                        (tokens and all(t in name for t in tokens))
                        or (raw_q and raw_q in barcode)
                    ):
                        continue
                matched.append(item)
            state["matched"] = matched

            # Best sellers are shown in the vertical rail now, so the grid is
            # a single flat result set (pos_catalog() still returns
            # best-sellers-first, so the ordering stays familiar).
            grid.controls = [self._item_tile(it) for it in matched]
            grid.visible = bool(matched)
            empty_box.visible = not matched

        empty_box = empty_state(
            "لا توجد نتائج",
            icon=ft.Icons.SEARCH_OFF,
            hint="جرّب اسمًا مختلفًا أو امسح باركود المادة مباشرة",
        )
        empty_box.visible = False

        def search_changed(e):
            state["query"] = search_field.value or ""
            render_grid()
            self.page.update()

        search_field.on_change = search_changed

        def search_submit(_=None):
            # Enter in the search box: when the Arabic-normalized query
            # narrows the catalog to exactly one item, add it without
            # reaching for the tile. Ambiguous queries keep the grid so the
            # cashier picks by eye.
            matched = state.get("matched") or []
            if len(matched) == 1 and not self._price_check_mode:
                self._add_item(int(matched[0]["id"]))
                search_field.value = ""
                state["query"] = ""
                render_grid()
                self.page.update()
            elif len(matched) == 1:
                self._show_price_check_card(matched[0], None, None, 1.0)

        search_field.on_submit = search_submit

        def barcode_submit(e):
            code = (barcode_field.value or "").strip()
            barcode_field.value = ""
            if code:
                self._add_by_barcode(code)
            # barcode_field no longer collapses the cart on focus (see
            # so this is just a safety net -- forces the cart visible after
            # every scan regardless of what else may have hidden it.
            cart_wrap.visible = True
            if not focus_state["modal_open"]:
                barcode_field.focus()
            self.page.update()

        barcode_field.on_submit = barcode_submit

        # ---- cart panel -------------------------------------------------
        # Bounded height + its own scroll: the cart panel is pinned fixed
        # (with the bottom pay bar) instead of scrolling away with the
        # catalog, so a long cart needs to scroll internally rather than
        # growing without limit.
        CART_FULL_HEIGHT = 220
        cart_column = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=CART_FULL_HEIGHT)
        total_text = ft.Text("0.00", size=22, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK)
        count_text = ft.Text("0 بند", size=12, color=Colors.TEXT_SECONDARY)
        bottom_count_text = ft.Text("0 بند", size=11, color=Colors.TEXT_FAINT)
        held_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO, wrap=False, visible=False)

        customer_dd = SearchSelect(
            label="عميل (اختياري)",
            hint_text="نقدي بدون عميل",
            choices=[(str(c["id"]), c["name"]) for c in customers],
        )

        received_display = ft.Text("0.00", size=22, weight=ft.FontWeight.BOLD)
        change_text = ft.Text("الباقي: 0.00", size=13, color=Colors.SUCCESS_DARK)
        auto_print_switch = ft.Switch(label="طباعة تلقائية بعد الدفع", value=pos_settings.auto_print_default(self.ctx.settings), scale=0.85)
        saved_layout, _saved_roll_width = saved_print_profile(self.ctx.settings, surface="pos")
        print_layout_toggle = SegmentedToggle(
            options=[
                SegmentOption("sheet", "ورق A4", ft.Icons.DESCRIPTION_OUTLINED),
                SegmentOption("roll", "حراري", ft.Icons.RECEIPT_LONG_OUTLINED),
            ],
            value=saved_layout,
        )

        def persist_print_layout(_=None):
            try:
                save_print_layout(self.ctx.settings, "pos", print_layout_toggle.value or "sheet")
            except Exception:
                pass

        print_layout_toggle.on_change = persist_print_layout
        print_layout_block = ft.Column(
            [
                ft.Text("نوع الطباعة", size=12, weight=ft.FontWeight.W_600),
                print_layout_toggle,
                ft.Text("يُحفظ لنقطة البيع فقط.", size=10, color=Colors.TEXT_FAINT),
            ],
            spacing=6,
            tight=True,
        )

        checkout_btn = hero_button("دفع", icon=ft.Icons.PAYMENTS_OUTLINED)

        def total_amount() -> float:
            return sum(_cart_line_price(row) * float(row["qty"]) for row in self.cart.values())

        def _receive_settings():
            """Settings view whose display currency matches the cash-entry mode.

            The received amount is entered in *some* currency; by default that is
            the app's display currency. ``_receive_currency`` lets the
            cashier flip to the other supported currency for this sale
            without touching admin settings.
            """
            return self._receive_settings_obj()

        def received_amount() -> float:
            # The cashier enters the cash handed over in the currency shown
            # in the payment sheet (display currency by default); convert it to the
            # stored USD unit so it can be compared against total_amount()
            # (also USD).
            try:
                typed = float(self.received_digits) if self.received_digits else None
            except ValueError:
                typed = None
            if typed is None:
                return total_amount()
            return currency.to_stored(typed, currency.get_effective_rate(_receive_settings()))

        def remove_by_swipe(row_key):
            self._remember_undo()
            self.cart.pop(row_key, None)
            if row_key in self.cart_order:
                self.cart_order.remove(row_key)
            refresh_cart()
            self.page.update()

        def _apply_change_text():
            total = total_amount()
            change = received_amount() - total
            if change >= 0:
                change_text.value = (
                    f"الباقي للعميل: {self._money_receive(change)}" if change > 0 else "المبلغ مضبوط بالتمام"
                )
                change_text.color = Colors.SUCCESS_DARK
            elif self.customer_id:
                # A shortfall is a valid credit sale once a customer is
                # selected -- InvoiceService already records total-paid as
                # a receivable on that customer (see _require_party_for_credit
                # in invoice_service.py). This is not an error state.
                change_text.value = f"دفعة جزئية — سيُسجَّل {self._money_receive(-change)} على حساب العميل"
                change_text.color = Colors.WARNING_DARK
            else:
                # No customer means InvoiceService forces paid=total
                # regardless of what was typed here (anonymous sales can't
                # carry a receivable) -- so an unselected customer with a
                # cash shortfall needs a stronger warning: the checkout
                # will still go through as "fully paid" even though the
                # cashier hasn't actually collected the full amount yet.
                change_text.value = f"⚠️ ناقص {self._money_receive(-change)} — لا يوجد عميل لتسجيل الباقي، اختر عميلًا أو أكمل المبلغ"
                change_text.color = Colors.DANGER_DARK

        def refresh_cart():
            # Whatever hid cart_wrap (search/barcode field focus -- see
            # collapse_cart_for_search below) should never outlive an
            # actual cart change: refresh_cart() runs after every add,
            # remove, undo, hold and resume, regardless of *how* that
            # change happened (grid tap, camera scan, hardware barcode
            # wedge...). Forcing visibility back on here -- instead of only
            # on the search/barcode field's on_blur -- means the cart can
            # no longer get stuck hidden just because the field that
            # triggered the add never actually lost focus (a hardware
            # scanner keeps the barcode field focused between scans; a
            # catalog tap while the search field is still focused doesn't
            # blur it either). self._cart_wrap is only set once cart_wrap
            # exists below -- the very first refresh_cart() call during
            # initial layout happens before that, so this is a no-op then.
            cw = getattr(self, "_cart_wrap", None)
            if cw is not None:
                cw.visible = True
            total = total_amount()
            # Read the cart-display policy once per refresh instead of
            # opening a settings query for every cart line.
            self._cart_show_stock = pos_settings.show_stock_in_cart(self.ctx.settings)
            self._cart_allow_negative = pos_settings.allow_negative_stock(self.ctx.settings)
            rows = []
            for row_key in self.cart_order:
                row = self.cart.get(row_key)
                if row is None:
                    continue
                card = self._cart_row(row, on_change=lambda: (refresh_cart(), self.page.update()))
                # Swipe-to-remove is safe here (unlike the parties/items
                # lists — see SWIPE_ACTIONS_NOTES.md): a cart line is a
                # pre-checkout, unsaved in-memory row that already has an
                # instant, no-confirmation "X" remove button right on the
                # row. The swipe just triggers that exact same removal, so
                # it introduces no new destructive action.
                rows.append(
                    ft.Dismissible(
                        key=f"cart-{row_key}",
                        content=card,
                        dismiss_direction=ft.DismissDirection.HORIZONTAL,
                        background=ft.Container(
                            content=ft.Row([ft.Icon(ft.Icons.DELETE_OUTLINE, color=Colors.WHITE, size=18), ft.Text("حذف", color=Colors.WHITE, size=11, weight=ft.FontWeight.W_600)], spacing=6, alignment=ft.MainAxisAlignment.START),
                            bgcolor=Colors.DANGER_DARK, border_radius=10, padding=ft.padding.symmetric(horizontal=14), alignment=ft.alignment.center_left,
                        ),
                        secondary_background=ft.Container(
                            content=ft.Row([ft.Text("حذف", color=Colors.WHITE, size=11, weight=ft.FontWeight.W_600), ft.Icon(ft.Icons.DELETE_OUTLINE, color=Colors.WHITE, size=18)], spacing=6, alignment=ft.MainAxisAlignment.END),
                            bgcolor=Colors.DANGER_DARK, border_radius=10, padding=ft.padding.symmetric(horizontal=14), alignment=ft.alignment.center_right,
                        ),
                        on_dismiss=lambda _, k=row_key: remove_by_swipe(k),
                    )
                )
            cart_column.controls = rows if rows else [empty_state("السلة فارغة", icon=ft.Icons.SHOPPING_CART_OUTLINED, hint="امسح باركود أو اضغط على مادة للإضافة")]
            total_text.value = self.money(total)
            count_text.value = f"{len(self.cart_order)} بند" if self.cart_order else "0 بند"
            bottom_count_text.value = count_text.value
            self.received_digits = ""
            received_display.value = self._money_receive(total)
            _apply_change_text()
            checkout_btn.disabled = not self.cart_order
            render_quick_cash()
            held_row.visible = bool(self.held)
            if self.held:
                held_row.controls = [
                    ft.Container(
                        ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.PERSON_OUTLINE if h.get("has_customer") else ft.Icons.PAUSE_CIRCLE_OUTLINE,
                                    size=15, color=Colors.WARNING_DARK,
                                ),
                                ft.Text(h["label"], size=11, weight=ft.FontWeight.W_600),
                            ],
                            spacing=4, tight=True,
                        ),
                        padding=ft.padding.symmetric(horizontal=10, vertical=6),
                        bgcolor=Colors.WARNING_BG,
                        border=ft.border.all(1, Colors.WARNING),
                        border_radius=20,
                        on_click=lambda _, idx=idx: resume_held(idx),
                    )
                    for idx, h in enumerate(self.held)
                ]

        # ---- amount received display -------------------------------------
        def _sync_received_display():
            received_display.value = (
                self._money_receive(received_amount())
                if self.received_digits
                else self._money_receive(total_amount())
            )
            _apply_change_text()

        # ---- quick cash buttons ------------------------------------------
        # Common round amounts a cashier can tap for the amount received --
        # the exact total plus the next 2-3 round
        # figures above it, sized to whichever currency is displayed (SYP
        # notes vs USD notes have very different natural denominations).
        quick_cash_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO, wrap=False)

        def set_received(amount_displayed: float | None):
            self.received_digits = "" if amount_displayed is None else currency.format_plain(amount_displayed)
            _sync_received_display()
            self.page.update()

        def _quick_cash_button(label: str, amount_displayed: float | None):
            return ft.Container(
                ft.Text(label, size=12, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK),
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                bgcolor=Colors.BACKGROUND_ALT,
                border=ft.border.all(1, Colors.BORDER),
                border_radius=20,
                ink=True,
                on_click=lambda _, a=amount_displayed: set_received(a),
            )

        def render_quick_cash():
            settings = _receive_settings()
            total_displayed = currency.to_display(total_amount(), currency.get_effective_rate(settings))
            is_usd = currency.get_display_currency(settings) == currency.DISPLAY_CURRENCY_USD
            steps = [1, 5, 10, 20, 50, 100] if is_usd else [1000, 5000, 10000, 25000, 50000, 100000]
            max_buttons = pos_settings.quick_cash_count(self.ctx.settings)
            buttons = [_quick_cash_button("المبلغ بالتمام", None)]
            if total_displayed > 0:
                seen: set[float] = set()
                for step in steps:
                    val = math.ceil(total_displayed / step) * step
                    if val <= total_displayed or val in seen:
                        continue
                    seen.add(val)
                    buttons.append(_quick_cash_button(currency.format_display_value(val, settings, with_symbol=False), val))
                    if len(seen) == max_buttons:
                        break
            quick_cash_row.controls = buttons

        # ---- actions: undo / hold / new -----------------------------------
        def undo_last(_=None):
            if not self.cart_order:
                return
            last_key = self.cart_order[-1]
            row = self.cart.get(last_key)
            if row is None:
                self.cart_order.pop()
                refresh_cart()
                self.page.update()
                return
            if row["qty"] > 1:
                row["qty"] -= 1
            else:
                self._remember_undo()
                self.cart.pop(last_key, None)
                self.cart_order.pop()
            refresh_cart()
            self.page.update()

        def clear_cart(keep_customer: bool = False, *, undoable: bool = True):
            if undoable and self.cart_order:
                self._remember_undo()
            self.cart = {}
            self.cart_order = []
            # A manual USD/SYP flip is per-sale: clear it with the cart so
            # the next sale starts on the app's display currency again.
            self._receive_currency = None
            if not keep_customer:
                customer_dd.value = None
                self.customer_id = None

        def hold_cart(_=None):
            if not self.cart_order:
                self.notify("السلة فارغة")
                return
            if len(self.held) >= 5:
                self.notify("الحد الأقصى 5 فواتير معلّقة — استرجع واحدة أولًا", kind="warning")
                return
            n_items = len(self.cart_order)
            total = total_amount()
            # Prefer the customer's name over the first item's name when one
            # is attached -- "أحمد · 3 بنود · 12:40" tells the cashier *who*
            # this ticket belongs to at a glance, which matters far more for
            # picking the right one back up than which item happened to be
            # scanned first.
            display_name = ""
            has_customer = bool(self.customer_id and customer_map.get(self.customer_id))
            try:
                if has_customer:
                    display_name = str(customer_map[self.customer_id]["name"])[:18]
                else:
                    display_name = str(self.cart[self.cart_order[0]]["item"].get("name") or "")[:18]
            except Exception:
                pass
            label = f"{display_name or 'سلة'} · {n_items} بند · {self.money(total)} · {time.strftime('%H:%M')}"
            self.held.append(
                {
                    "label": label,
                    "has_customer": has_customer,
                    "cart": copy.deepcopy(self.cart),
                    "order": list(self.cart_order),
                    "customer_id": self.customer_id,
                }
            )
            clear_cart()
            refresh_cart()
            self.notify(f"تم التعليق ({len(self.held)} معلّقة) — ابدأ بيعًا جديدًا أو استرجع من الشريط")
            self.page.update()

        def resume_held(idx: int):
            if idx < 0 or idx >= len(self.held):
                return
            if self.cart_order:
                # Auto-hold current cart so resume never loses work
                hold_cart()
                if self.cart_order:
                    self.notify("تعذّر تعليق السلة الحالية", kind="error")
                    return
            held_ticket = self.held.pop(idx)
            self.cart = copy.deepcopy(held_ticket["cart"])
            self.cart_order = list(held_ticket["order"])
            self.customer_id = held_ticket["customer_id"]
            customer_dd.value = str(self.customer_id) if self.customer_id else None
            try:
                customer_dd.update()
            except Exception:
                pass
            refresh_cart()
            self.notify("تم استرجاع السلة المعلّقة")
            self.page.update()

        # ---- checkout -----------------------------------------------------
        def do_checkout(_=None) -> bool:
            if not self.cart_order:
                self.notify("السلة فارغة")
                return False
            if getattr(self, "_checkout_in_progress", False):
                return False
            self._checkout_in_progress = True
            try:
                rows_list = [self.cart[i] for i in self.cart_order]
                # Margin checks compare a per-*base*-unit price with the
                # item's cost, while cart prices are per selected unit.
                margin_rows = [
                    {
                        **row,
                        "unit_price": _cart_line_price(row) / max(float(row.get("factor") or 1), 1e-9),
                    }
                    for row in rows_list
                ]
                margin_issues = cart_margin_warnings(margin_rows, self.ctx.settings)
                if margin_issues and not getattr(self, "_margin_override", False):
                    names = "، ".join(str(w.get("name") or "") for w in margin_issues[:3])
                    dialog = ft.AlertDialog(modal=True)

                    def proceed(_=None):
                        self.page.close(dialog)
                        self._margin_override = True
                        if do_checkout():
                            payment_sheet = getattr(self, "_payment_sheet", None)
                            if payment_sheet is not None:
                                try:
                                    self.page.close(payment_sheet)
                                except Exception:
                                    pass

                    def cancel(_=None):
                        self.page.close(dialog)

                    dialog.title = ft.Text("تحذير هامش الربح")
                    dialog.content = ft.Text(
                        f"بعض البنود تُباع تحت التكلفة أو بهامش منخفض:\n{names}\n\nهل تريد متابعة البيع؟"
                    )
                    dialog.actions = [
                        ft.TextButton("إلغاء", on_click=cancel),
                        ft.FilledButton("متابعة البيع", on_click=proceed),
                    ]
                    self.page.open(dialog)
                    return False
                self._margin_override = False
                lines = [
                    InvoiceLineInput(
                        description=str(row["item"]["name"]),
                        item_id=int(row["item"]["id"]),
                        quantity=float(row["qty"]),
                        unit_price=_cart_line_price(row),
                        unit_id=row.get("unit_id"),
                        conversion_factor=float(row.get("factor") or 1),
                    )
                    for row in rows_list
                ]
                total = total_amount()
                received = received_amount()
                paid = min(received, total) if self.customer_id else total
                invoice_id = self.ctx.invoices.create_invoice(
                    invoice_type="sale",
                    lines=lines,
                    customer_id=self.customer_id,
                    paid_amount=paid,
                    allow_negative_stock=pos_settings.allow_negative_stock(self.ctx.settings),
                )
                self._last_invoice_id = invoice_id
                change_amt = max(0.0, received - total)
                # The sale is committed from this point on. Clear the cart
                # immediately so a second click (or a UI glitch while showing
                # the success overlay) can never turn one sale into two.
                self._undo_state = None
                self._remember_recent_customer(self.customer_id)
                clear_cart(undoable=False)
                refresh_cart()
                refresh_today_summary()
                if auto_print_switch.value and self.native_files is not None:
                    self.page.run_task(self._print_receipt, invoice_id, interactive=False)
                try:
                    if self.on_saved:
                        self.on_saved()
                except Exception:
                    pass
                try:
                    self._show_sale_success(
                        invoice_id=invoice_id,
                        total=total,
                        received=received,
                        change=change_amt,
                        clear_cart_fn=clear_cart,
                        refresh_cart_fn=refresh_cart,
                        refresh_today_fn=refresh_today_summary,
                    )
                except Exception:
                    # The invoice already exists; never report a committed
                    # sale as a failure just because the success overlay
                    # failed to render.
                    self.notify(
                        f"تم البيع بنجاح — فاتورة #{invoice_id}",
                        kind="success",
                        sound_kind="save",
                    )
                return True
            except Exception as exc:
                self.notify(str(exc), kind="error")
                return False
            finally:
                self._checkout_in_progress = False

        # ---- payment sheet --------------------------------------------
        # Everything needed to actually take money (customer, quick
        # cash, auto-print) used to sit permanently inside the cart column,
        # pushing "دفع" itself to the bottom of a long scroll once the cart
        # had more than a couple of lines. It now opens on demand from the
        # sticky total bar instead, so it only takes over the screen the
        # moment the cashier is actually paying.
        # is_scroll_controlled + maintain_bottom_view_insets_padding: same
        # fix as new_form_sheet() (components/form_sheet.py) -- without
        # is_scroll_controlled, Flutter shrink-wraps a modal BottomSheet to
        # a ~9/16-screen ceiling, and this sheet's content (customer field +
        # totals + change text + quick-cash row + print switch +
        # confirm button) is tall enough to blow past that on most phones.
        # The confirm button was landing below that ceiling with no scroll
        # to reach it -- effectively invisible/unusable in quick sale.
        payment_sheet = ft.BottomSheet(
            content=ft.Container(),
            is_scroll_controlled=True,
            enable_drag=True,
            maintain_bottom_view_insets_padding=True,
        )
        self._payment_sheet = payment_sheet
        payment_sheet.on_dismiss = _resume_barcode_focus

        def confirm_payment(_=None):
            if do_checkout():
                self.page.close(payment_sheet)

        def open_payment_sheet(_=None):
            if not self.cart_order:
                self.notify("السلة فارغة")
                return
            # Reset received to exact total for a one-tap confirm path.
            self.received_digits = ""
            _sync_received_display()
            render_quick_cash()
            body = ft.Column(spacing=10, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

            def pick_recent_customer(cid: int):
                customer_dd.value = str(cid)
                customer_changed()

            def recent_customers_row() -> ft.Control:
                # Phase C: "last 3 customers" quick-pick under the customer
                # field -- a repeat customer's next sale shouldn't need a
                # full dropdown search every time. Silently hides itself
                # once nothing recent qualifies (session just started, or
                # every recent id has since been deleted).
                chips = []
                for cid in self.recent_customer_ids[:3]:
                    customer = customer_map.get(cid)
                    if customer is None:
                        continue
                    active = self.customer_id == cid
                    chips.append(
                        ft.Container(
                            ft.Text(str(customer["name"])[:16], size=11, weight=ft.FontWeight.W_600,
                                     color=Colors.WHITE if active else Colors.PRIMARY_DARK),
                            padding=ft.padding.symmetric(horizontal=12, vertical=7),
                            bgcolor=Colors.PRIMARY if active else Colors.WHITE,
                            border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
                            border_radius=18,
                            ink=True,
                            on_click=lambda _, c=cid: pick_recent_customer(c),
                        )
                    )
                if not chips:
                    return ft.Container(height=0)
                return ft.Column(
                    [
                        ft.Text("آخر العملاء", size=10, color=Colors.TEXT_FAINT),
                        ft.Row(chips, spacing=6, scroll=ft.ScrollMode.AUTO, wrap=False),
                    ],
                    spacing=4,
                )

            currency_toggle_row = ft.Row(spacing=6, alignment=ft.MainAxisAlignment.CENTER)

            def render_currency_toggle():
                app_currency = currency.get_display_currency(self.ctx.settings)
                options = [currency.DISPLAY_CURRENCY_SYP, currency.DISPLAY_CURRENCY_USD]
                if app_currency not in options:
                    options.insert(0, app_currency)
                active_currency = self._receive_currency or app_currency
                chips = []
                for code in options:
                    active = code == active_currency
                    label = "ل.س" if code == currency.DISPLAY_CURRENCY_SYP else "$"
                    chips.append(
                        ft.Container(
                            ft.Text(label, size=12, weight=ft.FontWeight.BOLD,
                                    color=Colors.WHITE if active else Colors.TEXT_MUTED),
                            padding=ft.padding.symmetric(horizontal=14, vertical=6),
                            bgcolor=Colors.PRIMARY if active else Colors.BACKGROUND_ALT,
                            border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
                            border_radius=16,
                            ink=True,
                            on_click=lambda _, c=code: switch_receive_currency(c),
                        )
                    )
                currency_toggle_row.controls = chips

            def switch_receive_currency(code: str):
                app_currency = currency.get_display_currency(self.ctx.settings)
                self._receive_currency = None if code == app_currency else code
                # The digits were typed in the previous currency; resetting
                # to "exact total" avoids silently reinterpreting 50 as $50
                # when the cashier meant 50,000 ل.س.
                self.received_digits = ""
                render_currency_toggle()
                render_quick_cash()
                _sync_received_display()
                try:
                    body.update()
                except Exception:
                    self.page.update()

            customer_insight = ft.Container(visible=False)

            def render_customer_insight():
                customer = customer_map.get(self.customer_id) if self.customer_id else None
                balance = float((customer or {}).get("balance") or 0)
                if customer is None or abs(balance) <= 0.005:
                    customer_insight.visible = False
                    return
                owes = balance > 0
                customer_insight.content = ft.Row(
                    [
                        ft.Icon(
                            ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
                            size=16,
                            color=Colors.WARNING_DARK if owes else Colors.SUCCESS_DARK,
                        ),
                        ft.Text(
                            (
                                f"على العميل {self.money(balance)}"
                                if owes
                                else f"له رصيد دائن {self.money(-balance)}"
                            ),
                            size=11,
                            weight=ft.FontWeight.W_600,
                            color=Colors.WARNING_DARK if owes else Colors.SUCCESS_DARK,
                        ),
                    ],
                    spacing=6,
                )
                customer_insight.padding = ft.padding.symmetric(horizontal=12, vertical=8)
                customer_insight.bgcolor = Colors.WARNING_BG if owes else Colors.SUCCESS_BG
                customer_insight.border_radius = 12
                customer_insight.visible = True

            self._render_payment_customer_insight = render_customer_insight

            confirm_btn = hero_button("إتمام وطباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=confirm_payment)
            confirm_btn.expand = True

            def sync_confirm_label(_=None):
                printing = bool(auto_print_switch.value)
                confirm_btn.text = "إتمام وطباعة" if printing else "إتمام"
                confirm_btn.icon = ft.Icons.PRINT_OUTLINED if printing else ft.Icons.POINT_OF_SALE_OUTLINED
                try:
                    confirm_btn.update()
                except Exception:
                    pass

            auto_print_switch.on_change = sync_confirm_label
            sync_confirm_label()

            body.controls = [
                ft.Text(self.money(total_amount()), size=28, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK),
                ft.Text("الإجمالي المطلوب", size=11, color=Colors.TEXT_SECONDARY),
                ft.Container(height=4),
                currency_toggle_row,
                received_display,
                change_text,
                quick_cash_row,
                ft.Divider(height=1, color=Colors.BORDER),
                ft.Text("العميل (اختياري)", size=12, weight=ft.FontWeight.W_600, color=Colors.TEXT_SECONDARY),
                ft.Text("اتركه فارغًا للبيع النقدي السريع", size=11, color=Colors.TEXT_FAINT),
                customer_dd,
                customer_insight,
                recent_customers_row(),
                ft.Divider(height=1, color=Colors.BORDER),
                auto_print_switch,
                print_layout_block,
                ft.Row(
                    [
                        ft.OutlinedButton("إلغاء", on_click=lambda _: self.page.close(payment_sheet), expand=True),
                        confirm_btn,
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ]
            render_currency_toggle()
            render_customer_insight()
            payment_sheet.content = ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Text("إتمام الدفع", size=18, weight=ft.FontWeight.BOLD),
                        body,
                    ],
                    spacing=12,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
                padding=ft.padding.only(left=18, right=18, top=12, bottom=28),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=24, top_right=24),
            )
            _mark_overlay_open()
            self.page.open(payment_sheet)

        checkout_btn.on_click = open_payment_sheet
        self._checkout_btn = checkout_btn

        def customer_changed(_=None):
            self.customer_id = int(customer_dd.value) if customer_dd.value else None
            # A shortfall's meaning (blocking warning vs. valid credit sale)
            # depends on whether a customer is selected, so re-evaluate the
            # change/credit message the moment that changes.
            _apply_change_text()
            render_insight = getattr(self, "_render_payment_customer_insight", None)
            if callable(render_insight):
                render_insight()
            self.page.update()

        customer_dd.on_change = customer_changed

        # ---- assemble layout ------------------------------------------
        render_categories()
        render_bestsellers()
        render_grid()
        refresh_cart()

        catalog_pane = ft.Column(
            [
                ft.Container(
                    ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=14, color=Colors.PRIMARY),
                                    ft.Text("جاهز لقارئ الباركود", size=10, color=Colors.PRIMARY_DARK),
                                ],
                                spacing=5,
                                tight=True,
                            ),
                            ft.Row(
                                [
                                    barcode_field,
                                    ft.IconButton(
                                        icon=ft.Icons.CAMERA_ALT_OUTLINED,
                                        tooltip="مسح بالكاميرا",
                                        on_click=scan_button_handler,
                                        icon_color=Colors.PRIMARY,
                                        visible=supports("barcode_camera_scan"),
                                    ),
                                ],
                                spacing=8,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ],
                        spacing=5,
                    ),
                    padding=10,
                    bgcolor=Colors.PRIMARY_BG,
                    border=ft.border.all(1, Colors.PRIMARY_BORDER),
                    border_radius=Radius.MD,
                ),
                ft.Row([search_field], spacing=10),
                recent_toggle,
                recent_row,
                grid,
                empty_box,
            ],
            spacing=10,
        )

        # Cart pane keeps only what a cashier needs while still browsing --
        # customer, received-amount quick cash and auto-print all
        # moved into the payment sheet (see open_payment_sheet above), so
        # this column stays short even with several lines in the cart.
        hold_btn = ft.IconButton(icon=ft.Icons.PAUSE_CIRCLE_OUTLINED, tooltip="تعليق الفاتورة", on_click=hold_cart, icon_size=IconSize.HEADER)
        undo_btn = ft.IconButton(icon=ft.Icons.UNDO, tooltip="تراجع عن آخر إضافة", on_click=undo_last, icon_size=IconSize.HEADER)
        price_check_btn = ft.IconButton(
            icon=ft.Icons.PRICE_CHECK,
            tooltip="فحص السعر (F7) — امسح دون إضافة للسلة",
            on_click=lambda _: toggle_price_check(),
            icon_size=IconSize.HEADER,
        )
        recall_btn = ft.IconButton(
            icon=ft.Icons.REPLAY,
            tooltip="إعادة آخر فاتورة إلى السلة",
            on_click=lambda _: recall_last_sale(),
            icon_size=IconSize.HEADER,
        )
        print_last_btn = ft.IconButton(
            icon=ft.Icons.PRINT_OUTLINED,
            tooltip="طباعة آخر فاتورة (F12)",
            on_click=lambda _: self._print_last_receipt(),
            icon_size=IconSize.HEADER,
        )

        def toggle_price_check():
            self._price_check_mode = not self._price_check_mode
            price_check_btn.icon = (
                ft.Icons.POINT_OF_SALE_OUTLINED if self._price_check_mode else ft.Icons.PRICE_CHECK
            )
            price_check_btn.icon_color = Colors.WARNING_DARK if self._price_check_mode else None
            self.notify(
                "وضع فحص السعر: امسح لعرض السعر فقط" if self._price_check_mode
                else "وضع البيع: المسح يضيف للسلة"
            )
            self.page.update()

        def recall_last_sale():
            if self.cart_order:
                self.notify("السلة غير فارغة — أفرغها أو علّقها أولًا", kind="warning")
                return
            try:
                recent = self.ctx.invoices.list_invoices("sale", limit=1)
                invoice = self.ctx.invoices.get_invoice(int(recent[0]["id"])) if recent else None
            except Exception:
                invoice = None
            if not invoice:
                self.notify("لا توجد فاتورة سابقة")
                return
            added = 0
            missing = 0
            for line in invoice.get("lines") or []:
                item_id = line.get("item_id")
                item = self.item_map.get(int(item_id)) if item_id is not None else None
                if item is None:
                    missing += 1
                    continue
                qty = float(line.get("quantity") or 0)
                if qty <= 0:
                    continue
                unit_id = line.get("unit_id")
                factor = float(line.get("conversion_factor") or 1)
                self._add_item(
                    int(item_id),
                    qty,
                    float(line.get("unit_price") or item["selling_price"]),
                    unit_id=int(unit_id) if unit_id is not None else None,
                    factor=factor,
                )
                added += 1
            if invoice.get("customer_id"):
                self.customer_id = int(invoice["customer_id"])
                customer_dd.value = str(self.customer_id)
                customer_changed()
            note = "تم استرجاع آخر فاتورة" if added else "تعذّر استرجاع بنود آخر فاتورة"
            if missing:
                note += f" ({missing} مادة غير متوفرة)"
            self.notify(note, kind="warning" if missing else None)
            self.page.update()

        # Running total + pay action now live in a pinned footer *inside* the
        # cart (glued to the receipt they belong to) instead of a full-width
        # bar under the whole screen. The catalog regains that height and the
        # cashier's eye never leaves the basket to pay.
        cart_footer = ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("الإجمالي", size=12, color=Colors.TEXT_SECONDARY),
                            ft.Container(expand=True),
                            bottom_count_text,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    total_text,
                    ft.Row([checkout_btn], spacing=0),
                ],
                spacing=6,
            ),
            padding=ft.padding.only(top=10),
            border=ft.border.only(top=ft.BorderSide(1, Colors.BORDER)),
        )
        checkout_btn.expand = True

        cart_pane = ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("السلة", size=16, weight=ft.FontWeight.BOLD, expand=True),
                            count_text,
                            ft.Row([price_check_btn, recall_btn, print_last_btn], spacing=0, tight=True),
                            hold_btn,
                            undo_btn,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    held_row,
                    cart_column,
                    cart_footer,
                ],
                spacing=8,
                expand=True,
            ),
            padding=14,
            bgcolor=Colors.WHITE,
            border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.LG,
            shadow=Shadow.MD,
        )

        # Vertical side rail: categories stacked on top, then the best-seller
        # quick-add list. Fixed width so the product grid keeps the flexible
        # middle and scrolling a long grid never drags the categories away.
        catalog_rail = ft.Container(
            ft.Column(
                [
                    ft.Text("التصنيفات", size=12, weight=ft.FontWeight.BOLD, color=Colors.TEXT_SECONDARY),
                    cat_list,
                    best_toggle,
                    best_list,
                ],
                spacing=6,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            width=210,
            padding=ft.padding.symmetric(horizontal=10, vertical=12),
            bgcolor=Colors.WHITE,
            border=ft.border.all(1, Colors.BORDER),
            border_radius=Radius.LG,
            shadow=Shadow.SM,
        )

        # Compact fullscreen header: replaces the app's own top bar (hidden
        # via on_fullscreen_enter) with an exit button + title + the "how's
        # today going" glance, in one dense row. POS is voice-free and has no
        # flow/full toggle any more (always the full toolset), so the header
        # is just exit + identity -- nothing else competes for the space.
        pos_header = ft.Container(
            ft.Row(
                [
                    ft.IconButton(icon=ft.Icons.ARROW_FORWARD_IOS if self.page.rtl else ft.Icons.ARROW_BACK_IOS_NEW, icon_size=IconSize.HEADER, tooltip="إنهاء البيع السريع", on_click=self._exit_pos),
                    ft.Column(
                        [ft.Text("نقطة البيع", size=15, weight=ft.FontWeight.BOLD), today_summary_text],
                        spacing=0, expand=True,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=6,
            ),
            padding=ft.padding.only(left=14, right=14, top=10, bottom=10),
            bgcolor=Colors.WHITE,
            border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
        )

        # Wrapped so it can be hidden as a unit (see collapse_cart_for_search
        # below) without touching cart_pane's own children/state.
        cart_wrap = ft.Container(cart_pane, padding=ft.padding.only(left=18, right=18, bottom=8))
        # Exposed on self so refresh_cart() (defined above, called after
        # every cart mutation) can force it back visible regardless of
        # focus/blur -- see the comment at the top of refresh_cart().
        self._cart_wrap = cart_wrap

        # ---- desktop layout ----------------------------------------------
        # The cart is a full-height side rail next to an independently-
        # scrolling catalog, so the cashier sees the whole receipt + running
        # total without scrolling. (The app is desktop-only -- there is no
        # stacked mobile layout any more.)
        self._pos_wide = True
        if self._pos_wide:
            # Rail sizing: no fixed height and no collapse-on-search (there
            # is no on-screen keyboard to dodge on desktop); the rail just
            # fills whatever height the body row gives it.
            cart_wrap.width = CART_RAIL_WIDTH
            cart_wrap.padding = ft.padding.only(left=16, right=16, bottom=16)
            cart_pane.expand = True
            cart_pane.content.expand = True
            cart_column.expand = True
            cart_column.height = None

        # The cart rail never shrinks on search focus -- focusing search just
        # refines the catalog, it does not collapse the receipt the cashier
        # is reading.

        # Only the catalog scrolls now. The cart pane used to sit inside
        # this same scrolling body, which meant scrolling through the catalog
        # grid could scroll the cart out of view too. The cart is pulled out
        # as a fixed side rail instead, so it (and the total/checkout button)
        # stays reachable no matter how far the catalog is scrolled.
        scroll_body = ft.Column(
            [catalog_pane],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

        # Three columns, each stretching to the full body height and scrolling
        # on its own: the vertical category/best-seller rail, the product
        # catalog, then the cart rail. The body itself never scrolls, so no
        # pane can push another out of view.
        body = ft.Row(
            [
                catalog_rail,
                ft.Container(
                    scroll_body,
                    padding=ft.padding.only(left=16, right=16, top=14, bottom=10),
                    expand=True,
                ),
                cart_wrap,
            ],
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            expand=True,
        )

        main_column = ft.Column(
            [
                pos_header,
                body,
            ],
            spacing=0,
            expand=True,
        )

        # Phase B overlays: last-scan floating card (top) + full success screen
        # last_scan_overlay is positioned via top/left/right (real Stack
        # positioning) rather than padding+alignment: a Container with
        # alignment set expands to fill whichever constraint axis its
        # parent gives it as *bounded* -- inside a plain (non-positioned)
        # Stack child that axis is bounded in both width AND height, which
        # was stretching this down the full screen height instead of
        # shrink-wrapping to the small floating card inside it. Setting
        # left+right (bounded width band to center the card in) while
        # leaving bottom unset (unbounded height) keeps the width behavior
        # we want while letting the height shrink to the card's own
        # intrinsic size, matching how it renders in _show_last_scan_card().
        last_scan_overlay = ft.Container(
            visible=False,
            content=ft.Container(),
            top=72,
            left=20,
            right=20,
            alignment=ft.alignment.top_center,
            opacity=0,
            animate_opacity=ft.Animation(180, ft.AnimationCurve.EASE_OUT),
        )
        success_overlay = ft.Container(
            visible=False,
            expand=True,
            bgcolor="#CC0F172A",  # dim scrim
            alignment=ft.alignment.center,
            content=ft.Container(),
        )
        self._last_scan_overlay = last_scan_overlay
        self._success_overlay = success_overlay

        self.content.content = ft.Stack(
            [main_column, last_scan_overlay, success_overlay],
            expand=True,
        )

        # Let the shell route resize events here while POS is fullscreen:
        # adapt_navigation() delegates through this hook instead of restoring
        # chrome. The hook is a no-op on desktop (the rail layout is fixed),
        # but stays wired so the shell always has a valid callable.
        try:
            self.page._nano_pos_relayout = self._pos_handle_resize
        except Exception:
            pass
        self.page.update()
        try:
            self._prev_on_keyboard = getattr(self.page, "on_keyboard_event", None)
        except Exception:
            self._prev_on_keyboard = None
        def _pos_keyboard(e):
            if focus_state["modal_open"]:
                return
            try:
                k = (e.key or "").strip().lower().replace(" ", "")
                if k == "escape" or k == "esc":
                    # Double-press guard: a single stray Esc must never
                    # wipe a half-built cart. First press arms for 3s,
                    # second press within that window clears (F8 restores).
                    now = time.monotonic()
                    if now - self._esc_armed_at <= 3.0:
                        self._esc_armed_at = 0.0
                        clear_cart()
                        self.notify("تم تفريغ السلة — F8 للتراجع")
                    else:
                        self._esc_armed_at = now
                        self.notify("اضغط Esc مرة أخرى خلال 3 ثوانٍ لتفريغ السلة")
                    self.page.update()
                    return
                if k == "f2":
                    checkout_btn.on_click(None)
                    return
                if k == "f3":
                    search_field.focus()
                    return
                if k == "f4":
                    hold_cart(None)
                    return
                if k == "f7":
                    toggle_price_check()
                    return
                if k == "f8":
                    if not self._restore_undo():
                        self.notify("لا يوجد إجراء للتراجع عنه")
                    return
                if k == "f12" and pos_settings.f12_print_enabled(self.ctx.settings):
                    self._print_last_receipt()
                    return
                if k == "enter" and (e.ctrl or e.meta):
                    checkout_btn.on_click(None)
                    return
            except Exception:
                pass
        self.page.on_keyboard_event = _pos_keyboard

        # expose closures needed by helper methods below
        self._render_grid = render_grid
        self._refresh_cart = refresh_cart
        self._search_field = search_field
        self._barcode_field = barcode_field
        self._recent_toggle = recent_toggle
        self._recent_row = recent_row
        self._render_recent = render_recent
        self._refresh_today_summary = refresh_today_summary
        self._clear_cart = clear_cart
        self._total_amount = total_amount
        self._received_amount = received_amount
        self._do_checkout = do_checkout

        # Hands-free voice bridge: the voice session calls these two hooks
        # when it is already on the POS screen (or navigates here and then
        # replays the queued command, see _pending_voice_command).
        self._prev_voice_apply = getattr(self.ctx, "_pos_apply_voice", None)
        self._prev_voice_snapshot = getattr(self.ctx, "_pos_cart_snapshot", None)
        try:
            self.ctx._pos_apply_voice = self._apply_voice_command
            self.ctx._pos_cart_snapshot = self._cart_snapshot
        except Exception:
            pass
        pending = getattr(self.ctx, "_pending_voice_command", None)
        if pending is not None:
            try:
                self.ctx._pending_voice_command = None
            except Exception:
                pass
            self.page.run_task(self._apply_pending_voice, pending)

    # ------------------------------------------------------------------ #
    # Small UI helpers
    # ------------------------------------------------------------------ #

    def _rail_category(self, label: str, active: bool, color: str, icon: str, on_click) -> ft.Container:
        """A single row in the vertical category rail.

        Active state fills the whole row in the category colour (icon tile +
        text) so the current filter reads at a glance without a separate
        highlight bar.
        """
        return ft.Container(
            ft.Row(
                [
                    ft.Container(
                        ft.Icon(icon, size=16, color=Colors.WHITE if active else color),
                        width=30, height=30, alignment=ft.alignment.center,
                        bgcolor=Colors.WHITE if active else Colors.BACKGROUND_ALT,
                        border_radius=9,
                    ),
                    ft.Text(
                        label, size=12, weight=ft.FontWeight.W_600,
                        color=Colors.WHITE if active else Colors.TEXT_MUTED_DARK,
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True,
                    ),
                ],
                spacing=8, tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            bgcolor=color if active else Colors.WHITE,
            border=ft.border.all(1, color if active else Colors.BORDER),
            border_radius=Radius.SM,
            on_click=on_click,
            ink=True,
        )

    def _rail_product(self, item: dict) -> ft.Container:
        """A quick-add row in the rail's best-seller list: name + price on
        the left, a subtle add affordance on the right -- one tap drops it
        straight into the cart."""
        is_service = item["item_type"] == "خدمة"
        return ft.Container(
            ft.Row(
                [
                    ft.Icon(
                        ft.Icons.DESIGN_SERVICES_OUTLINED if is_service else ft.Icons.INVENTORY_2_OUTLINED,
                        size=15, color=Colors.PRIMARY,
                    ),
                    ft.Column(
                        [
                            ft.Text(item["name"], size=11.5, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(self.money(item["selling_price"]), size=11, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK),
                        ],
                        spacing=0, expand=True, alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE, size=18, color=Colors.PRIMARY),
                ],
                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            bgcolor=Colors.BACKGROUND_ALT,
            border_radius=9,
            on_click=lambda _, i=int(item["id"]): self._add_item(i),
            ink=True,
        )

    def _item_tile(self, item: dict) -> ft.Container:
        is_service = item["item_type"] == "خدمة"
        qty_badge = None
        out_of_stock = False
        if not is_service:
            available = float(item.get("quantity") or 0)
            out_of_stock = available <= 0
            badge_color = Colors.DANGER_DARK if out_of_stock else Colors.TEXT_FAINT
            qty_badge = ft.Text(f"م: {self._qty(item['quantity'])}", size=10, color=badge_color, weight=ft.FontWeight.BOLD if out_of_stock else None)
        idx = 0
        # stable-ish color by category id for a touch of visual grouping
        if item.get("category_id"):
            idx = int(item["category_id"])
        color = CHIP_PALETTE[idx % len(CHIP_PALETTE)]
        return ft.Container(
            ft.Column(
                [
                    ft.Container(
                        ft.Icon(ft.Icons.INVENTORY_2_OUTLINED if not is_service else ft.Icons.DESIGN_SERVICES_OUTLINED, color=color, size=26),
                        width=44, height=44, alignment=ft.alignment.center, bgcolor=Colors.BACKGROUND_ALT, border_radius=12,
                    ),
                    ft.Text(item["name"], size=12, weight=ft.FontWeight.W_600, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.CENTER),
                    ft.Text(self.money(item["selling_price"]), size=13, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK),
                    qty_badge or ft.Container(height=1),
                ],
                spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=10,
            bgcolor=Colors.WHITE,
            # Out-of-stock is a nudge, not a block -- the sale is still
            # allowed (e.g. a known backorder), just flagged visually via
            # the border so the cashier notices before scanning more.
            border=ft.border.all(1.5 if out_of_stock else 1, Colors.DANGER if out_of_stock else Colors.BORDER),
            border_radius=Radius.MD,
            ink=True,
            col={"xs": 6, "sm": 4, "md": 4, "lg": 3, "xl": 2},
            on_click=lambda _, i=int(item["id"]): self._add_item(i),
        )

    def _cart_row(self, row: dict, on_change) -> ft.Container:
        item = row["item"]
        is_service = item["item_type"] == "خدمة"
        is_weighted = (not is_service) and self._is_weighted_item(item)

        row_key = row.get("key", int(item["id"]))

        def change_qty(delta: float):
            new_qty = row["qty"] + delta
            if new_qty <= 0:
                self._remember_undo()
                self.cart.pop(row_key, None)
                if row_key in self.cart_order:
                    self.cart_order.remove(row_key)
            else:
                row["qty"] = new_qty
            on_change()

        def set_qty(value: float):
            row["qty"] = value
            on_change()

        def edit_qty(_=None) -> None:
            # Tap the quantity to type an exact figure (e.g. 0.75 kg or 6
            # cartons) instead of stepping the ± buttons ten times.
            qty_field = SelectAllTextField(
                label="الكمية",
                value=self._qty(row["qty"]),
                keyboard_type=ft.KeyboardType.NUMBER,
                autofocus=True,
            )

            def apply(_=None) -> None:
                try:
                    value = float((qty_field.value or "").replace(",", "."))
                except Exception:
                    self.notify("قيمة غير صالحة", kind="error")
                    return
                if value <= 0:
                    self._remember_undo()
                    self.cart.pop(row_key, None)
                    if row_key in self.cart_order:
                        self.cart_order.remove(row_key)
                else:
                    row["qty"] = value
                self.page.close(qty_sheet)
                on_change()

            qty_sheet = ft.BottomSheet(
                content=ft.Container(),
                is_scroll_controlled=True,
                enable_drag=True,
                maintain_bottom_view_insets_padding=True,
            )
            qty_sheet.on_dismiss = getattr(self, "_pos_resume_barcode_focus", None)
            render_form_sheet(
                self.page,
                qty_sheet,
                title=f"كمية {item['name']}",
                fields=[qty_field],
                on_close=lambda _: self.page.close(qty_sheet),
                on_save=apply,
            )
            mark_overlay_open = getattr(self, "_pos_mark_overlay_open", None)
            if callable(mark_overlay_open):
                mark_overlay_open()
            self.page.open(qty_sheet)

        def qty_shortcut(label: str, value: float) -> ft.Container:
            active = float(row["qty"] or 0) == value
            return ft.Container(
                ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=Colors.WHITE if active else Colors.PRIMARY_DARK),
                padding=ft.padding.symmetric(horizontal=10, vertical=4),
                bgcolor=Colors.PRIMARY if active else Colors.BACKGROUND_ALT,
                border=ft.border.all(1, Colors.PRIMARY if active else Colors.BORDER),
                border_radius=12,
                ink=True,
                on_click=lambda _, v=value: set_qty(v),
            )

        def remove(_=None):
            self._remember_undo()
            self.cart.pop(row_key, None)
            if row_key in self.cart_order:
                self.cart_order.remove(row_key)
            on_change()

        line_unit_price = _cart_line_price(row)
        line_total = line_unit_price * float(row["qty"])
        unit_label = self._cart_unit_label(row)

        def edit_price(_=None) -> None:
            # v0.9.6: tap the price line of a cart row to override that
            # line's unit price. Recalculates the line total, cart total,
            # the «آخر مسح»-independent margin badge and the checkout
            # margin warning, because refresh_cart() re-runs _cart_row +
            # total_amount() and checkout uses row["unit_price"].
            #
            # The field reads/writes in the *display* currency (SYP or USD,
            # whatever the admin has selected), not the stored USD figure,
            # so the cashier sees the same units as the cart line above and
            # never has to mentally divide by the exchange rate. On save
            # the typed value is converted back to USD via
            # currency.parse_display_input() and stored in
            # row["unit_price"] (which is always USD); on display the
            # cart line goes through self.money() which converts USD →
            # display, so the round-trip is lossless.
            price_field = SelectAllTextField(
                label=currency.amount_field_label("سعر البيع", self.ctx.settings),
                value=currency.to_input_text(line_unit_price, self.ctx.settings),
                keyboard_type=ft.KeyboardType.NUMBER,
                autofocus=True,
            )

            def apply(_=None) -> None:
                try:
                    new_price_usd = currency.parse_display_input(
                        price_field.value, self.ctx.settings
                    )
                except Exception:
                    self.notify("قيمة غير صالحة", kind="error")
                    return
                new_price_usd = max(0.0, new_price_usd)
                row["unit_price"] = new_price_usd
                self.page.close(price_sheet)
                on_change()

            price_sheet = ft.BottomSheet(
                content=ft.Container(),
                is_scroll_controlled=True,
                enable_drag=True,
                maintain_bottom_view_insets_padding=True,
            )
            price_sheet.on_dismiss = getattr(self, "_pos_resume_barcode_focus", None)
            render_form_sheet(
                self.page,
                price_sheet,
                title="تعديل سعر البند",
                fields=[price_field],
                on_close=lambda _: self.page.close(price_sheet),
                on_save=apply,
            )
            mark_overlay_open = getattr(self, "_pos_mark_overlay_open", None)
            if callable(mark_overlay_open):
                mark_overlay_open()
            self.page.open(price_sheet)

        # Advisory only: the strict guard lives in AccountingRebuilder, and
        # the admin can allow overselling per surface. The on-hand figure is
        # hidden by default so the cart never advertises stock levels.
        factor = float(row.get("factor") or 1)
        base_qty = float(row["qty"]) * factor
        stock_qty = float(item.get("quantity") or 0)
        exceeds_stock = (not is_service) and base_qty > stock_qty
        show_stock = getattr(self, "_cart_show_stock", None)
        if show_stock is None:
            show_stock = pos_settings.show_stock_in_cart(self.ctx.settings)
        allow_negative = getattr(self, "_cart_allow_negative", None)
        if allow_negative is None:
            allow_negative = pos_settings.allow_negative_stock(self.ctx.settings)
        warning_line = None
        if exceeds_stock and show_stock:
            if allow_negative:
                warning_line = ft.Text(
                    f"المتوفر: {self._qty(stock_qty)} — سيُسجَّل مخزون سالب",
                    size=10, color=Colors.WARNING_DARK, weight=ft.FontWeight.W_600,
                )
            else:
                warning_line = ft.Text(
                    f"⚠️ المتوفر فعليًا: {self._qty(stock_qty)} فقط",
                    size=10, color=Colors.DANGER_DARK, weight=ft.FontWeight.W_600,
                )

        # Phase B: margin badge (below cost) + qty badge on cart lines.
        # Prices are stored per *selected unit*, so normalize to the base
        # unit before comparing with the item's per-base-unit cost.
        margin_info = check_sale_margin(
            unit_price_usd=line_unit_price / max(factor, 1e-9),
            item=item,
            settings=self.ctx.settings,
        )
        below_cost = bool(margin_info.get("flag") and margin_info.get("below_cost", margin_info.get("flag")))
        badges: list = []
        qty_val = float(row["qty"] or 0)
        if qty_val > 1:
            badges.append(
                ft.Container(
                    ft.Text(f"×{self._qty(qty_val).rstrip('0').rstrip('.')}", size=10, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                    padding=ft.padding.symmetric(horizontal=7, vertical=2),
                    bgcolor=Colors.PRIMARY,
                    border_radius=8,
                )
            )
        if below_cost:
            badges.append(
                ft.Container(
                    ft.Text("تحت التكلفة", size=9, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                    padding=ft.padding.symmetric(horizontal=7, vertical=2),
                    bgcolor=Colors.DANGER,
                    border_radius=8,
                )
            )

        name_row_controls = [
            ft.Text(item["name"], size=12, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
        ]
        if unit_label:
            name_row_controls.append(
                ft.Container(
                    ft.Text(unit_label, size=9, weight=ft.FontWeight.W_600, color=Colors.PRIMARY_DARK),
                    padding=ft.padding.symmetric(horizontal=6, vertical=2),
                    bgcolor=Colors.PRIMARY_BG, border_radius=6,
                )
            )
        if badges:
            name_row_controls.append(ft.Row(badges, spacing=4, tight=True))

        return ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Row(name_row_controls, spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                    # FIX_0.9.6 (build 25): this price line used
                                    # ft.Row(..., on_click=edit_price) -- ft.Row
                                    # has no on_click parameter in Flet 0.28.3,
                                    # so building ANY cart row raised TypeError
                                    # inside refresh_cart(), which _add_item()
                                    # calls after every grid tap / barcode scan.
                                    # Net effect: the quick-sale screen silently
                                    # failed to add items to the cart (Flet
                                    # swallows handler exceptions). Wrapped in
                                    # a Container, which is the tappable control
                                    # here, keeping the same tooltip + tap-to-
                                    # edit-price behavior.
                                    ft.Container(
                                        ft.Row(
                                            [
                                                ft.Icon(ft.Icons.EDIT_OUTLINED, size=11, color=Colors.TEXT_SECONDARY),
                                                ft.Text(
                                                    f"{self.money(line_unit_price)} × {self._qty(row['qty'])} = {self.money(line_total)}",
                                                    size=11,
                                                    color=Colors.TEXT_SECONDARY,
                                                ),
                                            ],
                                            spacing=4, tight=True,
                                        ),
                                        on_click=edit_price, tooltip="اضغط لتعديل السعر", ink=True,
                                    ),
                                ],
                                spacing=1, expand=True,
                            ),
                            stepper_icon_button(ft.Icons.REMOVE_CIRCLE_OUTLINE, lambda _: change_qty(-1)),
                            ft.Container(
                                ft.Text(self._qty(row["qty"]), size=12, weight=ft.FontWeight.BOLD),
                                padding=ft.padding.symmetric(horizontal=4, vertical=2),
                                ink=True,
                                tooltip="اضغط لكتابة الكمية",
                                on_click=edit_qty,
                            ),
                            stepper_icon_button(ft.Icons.ADD_CIRCLE_OUTLINE, lambda _: change_qty(1)),
                            stepper_icon_button(ft.Icons.CLOSE, remove, danger=True),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=2,
                    ),
                    *([warning_line] if warning_line else []),
                    # Phase C: for weighted/volume items, quick-set the
                    # actual weight instead of nudging by whole units with
                    # the +/- stepper (a scale reading like 0.5kg or 5kg
                    # isn't reachable one tap at a time). Sets the quantity
                    # outright rather than adding to it -- these are
                    # absolute reads, not increments.
                    *([ft.Row(
                        [qty_shortcut("½", 0.5), qty_shortcut("1", 1), qty_shortcut("5", 5), qty_shortcut("10", 10)],
                        spacing=6,
                    )] if is_weighted else []),
                ],
                spacing=2,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            bgcolor=Colors.DANGER_BG if below_cost else Colors.BACKGROUND_ALT,
            border=ft.border.all(1, Colors.DANGER) if ((exceeds_stock and show_stock and not allow_negative) or below_cost) else None,
            border_radius=10,
        )

    # ------------------------------------------------------------------ #
    # Cart mutation
    # ------------------------------------------------------------------ #

    def _add_item(
        self,
        item_id: int,
        qty_delta: float = 1.0,
        unit_price: float | None = None,
        *,
        unit_id: int | None = None,
        factor: float = 1.0,
    ) -> None:
        item = self.item_map.get(item_id)
        if item is None:
            return
        # Unit-aware identity: the same item sold by the piece and by the
        # carton are two distinct lines (different price per unit and
        # different stock factor), not one merged row.
        if unit_id is not None and int(unit_id) == int(item.get("base_unit_id") or 0):
            unit_id = None
        key = _cart_key(item, unit_id)
        row = self.cart.get(key)
        is_repeat = row is not None
        if row is None:
            self.cart[key] = {
                "key": key,
                "item": item,
                "qty": qty_delta,
                "unit_price": unit_price,
                "unit_id": unit_id,
                "factor": float(factor or 1.0),
            }
            self.cart_order.append(key)
            new_qty = qty_delta
        else:
            row["qty"] += qty_delta
            if unit_price is not None:
                row["unit_price"] = unit_price
            new_qty = row["qty"]
        self._remember_recent(item_id)
        self._refresh_cart()
        self._show_last_scan_card(item, new_qty, unit_price=unit_price, is_repeat=is_repeat)
        self.page.update()

    def _cart_unit_label(self, row: dict) -> str | None:
        """«كرتونة ×12» for a non-base unit line; ``None`` for base units."""
        unit_id = row.get("unit_id")
        factor = float(row.get("factor") or 1)
        if unit_id is None or factor == 1:
            return None
        try:
            unit = next(
                (u for u in self.ctx.items.units(int(row["item"]["id"])) if int(u["id"]) == int(unit_id)),
                None,
            )
        except Exception:
            unit = None
        name = str((unit or {}).get("name") or "وحدة")
        return f"{name} ×{self._qty(factor)}"

    # ------------------------------------------------------------------ #
    # Undo safety net / price-check mode
    # ------------------------------------------------------------------ #

    def _remember_undo(self) -> None:
        """Snapshot the cart before a destructive action (remove / clear).

        Kept deliberately tiny and in-memory only: one level, enough for
        the «تراجع» toast or F8 to bring back an accidentally removed line
        within the current sale. Checkout clears it.
        """
        if not self.cart_order:
            return
        self._undo_state = {
            "cart": copy.deepcopy(self.cart),
            "order": list(self.cart_order),
        }

    def _restore_undo(self) -> bool:
        state = self._undo_state
        if not state:
            return False
        self.cart = state["cart"]
        self.cart_order = list(state["order"])
        self._undo_state = None
        self._refresh_cart()
        self.notify("تم التراجع")
        self.page.update()
        return True

    def _voice_find_item(self, data: dict) -> dict | None:
        try:
            item_id = int(data.get("item_id") or 0)
        except Exception:
            item_id = 0
        if item_id:
            item = self.item_map.get(item_id)
            if item is not None:
                return item
        name = normalize_ar(str(data.get("name") or "").strip().casefold())
        if not name:
            return None
        for item in self.item_map.values():
            if normalize_ar(str(item.get("name") or "").casefold()) == name:
                return item
        for item in self.item_map.values():
            if name in normalize_ar(str(item.get("name") or "").casefold()):
                return item
        return None

    async def _apply_pending_voice(self, pending) -> None:
        # Deferred by one tick so the screen is fully attached before the
        # queued voice command mutates the cart (voice_session.navigate()
        # hands control back before the shell rebuilds the POS page).
        await asyncio.sleep(0)
        try:
            self._apply_voice_command(pending)
        except Exception:
            pass

    def _cart_snapshot(self) -> dict:
        lines = []
        for key in self.cart_order:
            row = self.cart.get(key)
            if row is None:
                continue
            lines.append({"name": row["item"].get("name"), "qty": float(row["qty"] or 0)})
        total_fn = getattr(self, "_total_amount", None)
        total = float(total_fn()) if callable(total_fn) else 0.0
        return {"count": len(lines), "total": total, "lines": lines}

    def _apply_voice_command(self, result) -> None:
        """Bridge for the hands-free voice session (see voice_session.py).

        The session resolves spoken item names / quantities and hands the
        resulting action here; this is the only place POS mutates its cart
        for voice, so every path keeps the same undo and refresh behavior
        as a tap or a scan.
        """
        action = getattr(result, "action", "")
        data = getattr(result, "data", {}) or {}
        if action == "pos_add":
            item = self._voice_find_item(data)
            if item is not None:
                self._add_item(int(item["id"]), float(data.get("qty") or 1))
            return
        if action == "pos_add_many":
            for entry in data.get("items") or []:
                item = self._voice_find_item(entry)
                if item is not None:
                    self._add_item(int(item["id"]), float(entry.get("qty") or 1))
            return
        if action == "pos_set_qty":
            qty = float(data.get("qty") or 0)
            item = self._voice_find_item(data)
            key = _cart_key(item, None) if item is not None else None
            if key not in self.cart and self.cart_order:
                key = self.cart_order[-1]
            row = self.cart.get(key)
            if row is None:
                return
            if qty <= 0:
                self._remember_undo()
                self.cart.pop(key, None)
                if key in self.cart_order:
                    self.cart_order.remove(key)
            else:
                row["qty"] = qty
            self._refresh_cart()
            self.page.update()
            return
        if action == "pos_remove_last":
            if self.cart_order:
                self._remember_undo()
                key = self.cart_order.pop()
                self.cart.pop(key, None)
                self._refresh_cart()
                self.page.update()
            return
        if action == "pos_clear":
            clear = getattr(self, "_clear_cart", None)
            if callable(clear):
                clear()
                self._refresh_cart()
                self.page.update()
            return
        if action == "pos_pay":
            checkout = getattr(self, "_checkout_btn", None)
            if checkout is not None:
                checkout.on_click(None)

    def _show_price_check_card(self, item: dict, unit_price: float | None, unit_id: int | None, factor: float) -> None:
        """F7 mode: scanning shows the price, it does NOT touch the cart."""
        overlay = getattr(self, "_last_scan_overlay", None)
        price = float(unit_price if unit_price is not None else item.get("selling_price") or 0)
        stock = item.get("quantity")
        subtitle_bits = [self.money(price)]
        if unit_id is not None and factor != 1:
            try:
                unit = next(
                    (u for u in self.ctx.items.units(int(item["id"])) if int(u["id"]) == int(unit_id)),
                    None,
                )
            except Exception:
                unit = None
            subtitle_bits.append(f"{str((unit or {}).get('name') or 'وحدة')} ×{self._qty(factor)}")
        subtitle_bits.append(f"المتوفر: {self._qty(stock)}")
        if overlay is not None:
            self._last_scan_token += 1
            token = self._last_scan_token
            overlay.content = ft.Container(
                ft.Row(
                    [
                        ft.Container(
                            ft.Icon(ft.Icons.PRICE_CHECK, color=Colors.WHITE, size=26),
                            width=44, height=44, border_radius=12,
                            bgcolor=Colors.PRIMARY, alignment=ft.alignment.center,
                        ),
                        ft.Column(
                            [
                                ft.Text(str(item.get("name") or ""), size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                ft.Text(" · ".join(subtitle_bits), size=12, color=Colors.TEXT_SECONDARY),
                            ],
                            spacing=2, expand=True,
                        ),
                    ],
                    spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=16, vertical=12),
                bgcolor=Colors.PRIMARY_BG,
                border=ft.border.all(1.5, Colors.PRIMARY),
                border_radius=16,
                shadow=Shadow.MD,
                width=340,
            )
            overlay.visible = True
            overlay.opacity = 1
            try:
                overlay.update()
            except Exception:
                pass

            async def _hide_after_delay():
                await asyncio.sleep(2.2)
                if token != self._last_scan_token:
                    return
                overlay.visible = False
                overlay.opacity = 0
                try:
                    overlay.update()
                except Exception:
                    pass

            self.page.run_task(_hide_after_delay)
        self.notify(
            f"{item.get('name')}: {self.money(price)} · المتوفر {self._qty(stock)}",
            sound_kind="scan",
        )

    def _show_last_scan_card(self, item: dict, qty: float, unit_price: float | None = None, is_repeat: bool = False) -> None:
        """Phase B: floating «آخر مسح» card — appears ~1.5s then fades away."""
        overlay = getattr(self, "_last_scan_overlay", None)
        if overlay is None:
            return
        self._last_scan_token += 1
        token = self._last_scan_token
        name = str(item.get("name") or "")
        price = self.money(float(unit_price) if unit_price is not None else float(item.get("selling_price") or 0))
        qty_label = f"×{self._qty(qty).rstrip('0').rstrip('.')}" if qty != 1 else ""
        subtitle = f"{price}  {qty_label}".strip()
        if is_repeat and qty > 1:
            subtitle = f"{price}  ·  الكمية {self._qty(qty)}"

        overlay.content = ft.Container(
            ft.Row(
                [
                    ft.Container(
                        ft.Icon(ft.Icons.CHECK_CIRCLE, color=Colors.WHITE, size=28),
                        width=44, height=44, border_radius=12,
                        bgcolor=Colors.SUCCESS, alignment=ft.alignment.center,
                    ),
                    ft.Column(
                        [
                            ft.Text(name, size=14, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text(subtitle, size=12, color=Colors.TEXT_SECONDARY),
                        ],
                        spacing=2, expand=True,
                    ),
                ],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=16, vertical=12),
            bgcolor=Colors.SUCCESS_BG,
            border=ft.border.all(1.5, Colors.SUCCESS),
            border_radius=16,
            shadow=Shadow.MD,
            # Explicit width so the card stays a compact floating chip --
            # see the note on last_scan_overlay's own top/left/right in
            # _build() for the actual full-screen-stretch bug this and
            # that positioning change fix together.
            width=320,
        )
        overlay.visible = True
        overlay.opacity = 1
        try:
            overlay.update()
        except Exception:
            pass

        async def _hide_after_delay():
            await asyncio.sleep(1.5)
            if self._last_scan_token != token:
                return  # a newer scan already replaced this card
            overlay.opacity = 0
            try:
                overlay.update()
            except Exception:
                pass
            # Let the fade-out finish before actually hiding -- otherwise
            # visible=False cuts the animation off mid-fade instead of
            # completing it.
            await asyncio.sleep(0.18)
            if self._last_scan_token != token:
                return
            overlay.visible = False
            try:
                overlay.update()
            except Exception:
                pass

        self.page.run_task(_hide_after_delay)

    def _show_sale_success(
        self,
        *,
        invoice_id: int,
        total: float,
        received: float,
        change: float,
        clear_cart_fn,
        refresh_cart_fn,
        refresh_today_fn,
    ) -> None:
        """Phase B: large success screen — amount · change · «بيع جديد»."""
        overlay = getattr(self, "_success_overlay", None)
        if overlay is None:
            # Fallback if layout not ready
            self.notify(
                f"تم البيع بنجاح — فاتورة #{invoice_id}"
                + (f" · الباقي {self.money(change)}" if change > 0 else "")
            )
            clear_cart_fn(undoable=False)
            refresh_cart_fn()
            refresh_today_fn()
            self.page.update()
            return

        # Hide last-scan card if still visible
        last = getattr(self, "_last_scan_overlay", None)
        if last is not None:
            last.visible = False
            last.opacity = 0

        async def print_sale(_=None):
            # Reprint the just-completed sale silently, matching the F12 /
            # auto-print behaviour -- no paper dialog in between.
            await self._print_receipt(invoice_id, interactive=False)

        def start_new_sale(_=None):
            clear_cart_fn(undoable=False)
            refresh_cart_fn()
            refresh_today_fn()
            overlay.visible = False
            self._success_visible = False
            try:
                overlay.update()
            except Exception:
                pass
            # Refocus barcode for the next scan
            bf = getattr(self, "_barcode_field", None)
            if bf is not None:
                try:
                    bf.focus()
                except Exception:
                    pass
            self.page.update()
            sound_engine.play(self.page, "scan")

        change_block = []
        if change > 0:
            change_block = [
                ft.Text("الباقي للعميل", size=14, color=Colors.TEXT_SECONDARY),
                ft.Text(self.money(change), size=36, weight=ft.FontWeight.BOLD, color=Colors.SUCCESS),
            ]
        elif received > 0 and abs(received - total) < 1e-6:
            change_block = [
                ft.Text("مبلغ مطابق", size=14, color=Colors.TEXT_SECONDARY),
            ]

        card = ft.Container(
            ft.Column(
                [
                    ft.Container(
                        ft.Icon(ft.Icons.CHECK_CIRCLE, color=Colors.WHITE, size=56),
                        width=88, height=88, border_radius=44,
                        bgcolor=Colors.SUCCESS, alignment=ft.alignment.center,
                    ),
                    ft.Container(height=12),
                    ft.Text("تم البيع بنجاح", size=22, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY),
                    ft.Text(f"فاتورة #{invoice_id}", size=13, color=Colors.TEXT_SECONDARY),
                    ft.Container(height=16),
                    ft.Text("المبلغ", size=13, color=Colors.TEXT_SECONDARY),
                    ft.Text(self.money(total), size=40, weight=ft.FontWeight.BOLD, color=Colors.PRIMARY_DARK),
                    *change_block,
                    ft.Container(height=20),
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "طباعة",
                                icon=ft.Icons.PRINT_OUTLINED,
                                on_click=print_sale,
                                height=52,
                                expand=True,
                            ),
                            hero_button(
                                "بيع جديد",
                                icon=ft.Icons.ADD_SHOPPING_CART,
                                on_click=start_new_sale,
                                expand=True,
                            ),
                        ],
                        spacing=8,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=28, vertical=32),
            bgcolor=Colors.WHITE,
            border_radius=24,
            shadow=Shadow.MD,
            width=340,
        )
        overlay.content = card
        overlay.visible = True
        self._success_visible = True
        try:
            sound_engine.play(self.page, "success")
        except Exception:
            pass
        try:
            overlay.update()
        except Exception:
            pass
        self.page.update()

    def _remember_recent(self, item_id: int) -> None:
        """Push ``item_id`` to the front of the session recall strip."""
        if item_id in self.recent_item_ids:
            self.recent_item_ids.remove(item_id)
        self.recent_item_ids.insert(0, item_id)
        del self.recent_item_ids[8:]  # short strip, not a history log
        render = getattr(self, "_render_recent", None)
        if render is not None:
            render()

    def _remember_recent_customer(self, customer_id: int | None) -> None:
        """Push ``customer_id`` to the front of the session recall strip.

        Called once a sale actually completes with a customer attached --
        not on every dropdown change -- so the suggestion reflects who was
        actually sold to, not just who was briefly selected then cleared.
        """
        if not customer_id:
            return
        customer_id = int(customer_id)
        if customer_id in self.recent_customer_ids:
            self.recent_customer_ids.remove(customer_id)
        self.recent_customer_ids.insert(0, customer_id)
        del self.recent_customer_ids[3:]  # only the last 3, per design

    @staticmethod
    def _is_weighted_item(item: dict) -> bool:
        """True when ``item``'s base unit looks like a weight/volume unit.

        Delegates to :func:`nano_offline.core.units.is_weighted_unit` so the
        POS and the LAN price server share one heuristic.
        """
        return is_weighted_unit(item.get("unit_abbreviation"), item.get("unit_name"))

    def _add_by_barcode(self, code: str) -> None:
        found = self.ctx.items.find_by_barcode(code)
        if not found:
            self._offer_create_item(code)
            return
        item_id = int(found["id"])
        self.item_map[item_id] = found
        # A code registered against a specific selling unit (e.g. a
        # carton barcode) should add that unit's full quantity in one
        # scan, not just 1 base unit -- see item_barcodes.unit_id /
        # ItemRepository.find_by_barcode.
        qty_delta = 1.0
        unit_price = None
        unit_id = None
        factor = 1.0
        matched_unit_id = found.get("matched_unit_id")
        if matched_unit_id:
            unit_row = next(
                (u for u in self.ctx.items.units(item_id) if int(u["id"]) == int(matched_unit_id)), None
            )
            if unit_row:
                # The unit barcode buys ONE unit of that unit (a carton),
                # priced per that unit -- not N base pieces at the carton
                # price. ``factor`` carries the stock conversion to the
                # invoice line; ``qty`` stays 1 so the line total is the
                # carton price, exactly like the invoice editor.
                unit_id = int(matched_unit_id)
                factor = float(unit_row.get("conversion_factor") or 1)
                _spu = unit_row.get("selling_price")
                unit_price = (
                    float(_spu)
                    if _spu not in (None, "")
                    else float(found.get("selling_price") or 0) * factor
                )
        # Real haptic feedback on scan would still need a native platform
        # channel added to the flet_native_files extension (Dart/Kotlin/
        # Swift) -- out of reach here with no Flutter toolchain or device to
        # verify against. The audible side of this is no longer a gap
        # though: this toast() call plays a real chime through
        # core/sound.py (audioplayers' AudioPool, no native code needed --
        # see its module docstring for why that's a different case than
        # vibration). It's pinned to the dedicated "scan" tone (sound_kind=
        # "scan") rather than the generic "success" chime a checkmark
        # message would otherwise infer -- a cashier scanning dozens of
        # items a minute gets a short, distinct, non-fatiguing tone tied
        # specifically to "item recognized", separate from success sounds
        # elsewhere in the app. The admin-configurable "الباركود" setting
        # below still picks how much on-screen detail this toast shows --
        # independent of the "الصوت" tab's own settings for whether/how
        # loud each tone kind plays.
        if self._price_check_mode:
            self._show_price_check_card(found, unit_price, unit_id, factor)
            return
        if barcode_settings.scan_feedback_mode(self.ctx.settings) == "brief":
            self.notify("✔", sound_kind="scan")
        else:
            qty_note = f" × {self._qty(factor)}" if unit_id is not None and factor != 1 else ""
            self.notify(f"✔ أُضيف: {found['name']}{qty_note}", sound_kind="scan")
        self._add_item(item_id, qty_delta, unit_price, unit_id=unit_id, factor=factor)

    def _offer_create_item(self, code: str) -> None:
        # A scan that matched nothing gets its own dedicated tone
        # (sound_kind="barcode_error") rather than the generic 'error'
        # chime -- distinct and short enough that a cashier scanning fast
        # immediately reads it as "no match" instead of "something broke",
        # whether or not this ends in the "create item?" dialog below.
        if self.on_create_item is None:
            self.notify("لا توجد مادة بهذا الباركود", kind="error", sound_kind="barcode_error")
            return
        sound_engine.play(self.page, "barcode_error")
        dialog = ft.AlertDialog(modal=True, title=ft.Text("لا توجد مادة بهذا الباركود"))
        dialog.on_dismiss = getattr(self, "_pos_resume_barcode_focus", None)

        def create(_=None) -> None:
            self.page.close(dialog)
            self.on_create_item(code)

        def cancel(_=None) -> None:
            self.page.close(dialog)

        dialog.content = ft.Text(f"الباركود الممسوح: {code}\nهل تريد إنشاء مادة جديدة بهذا الباركود؟")
        dialog.actions = [ft.TextButton("إلغاء", on_click=cancel), ft.FilledButton("إنشاء مادة جديدة", on_click=create)]
        mark_overlay_open = getattr(self, "_pos_mark_overlay_open", None)
        if callable(mark_overlay_open):
            mark_overlay_open()
        self.page.open(dialog)

    async def _scan_with_camera(self) -> None:
        if self.native_files is None:
            self.notify("مسح الباركود غير مهيأ في هذا البناء")
            return
        focus_state = getattr(self, "_pos_focus_state", None)
        if focus_state is not None:
            focus_state["modal_open"] = True
        try:
            code = await self.native_files.scan_barcode()
        except Exception as exc:
            self.notify(str(exc), kind="error")
            return
        finally:
            resume_focus = getattr(self, "_pos_resume_barcode_focus", None)
            if callable(resume_focus):
                resume_focus()
        if code:
            self._add_by_barcode(code)

    def _print_last_receipt(self) -> None:
        invoice_id = getattr(self, "_last_invoice_id", None)
        if not invoice_id:
            self.notify("لا توجد فاتورة مكتملة للطباعة — أكمل بيعًا أولًا", kind="warning")
            return
        if self.native_files is None:
            self.notify("الطباعة غير متاحة في هذا البناء", kind="warning")
            return
        self.page.run_task(self._print_receipt, int(invoice_id), interactive=False)

    async def _print_receipt(self, invoice_id: int, *, interactive: bool = True) -> None:
        if self.native_files is None:
            self.notify("الطباعة الأصلية غير مهيأة في هذا البناء")
            return

        def receipt_html() -> str:
            return self.ctx.documents.invoice_html(invoice_id)

        async def print_selected(layout: str, width: int) -> None:
            try:
                html = apply_print_profile(receipt_html(), layout, width)
                await self.native_files.print_html(
                    html,
                    name=f"nano-invoice-{invoice_id}",
                    paper_layout=layout,
                    roll_width_mm=width,
                    direct=True,
                )
                try:
                    session = self.ctx.auth.current()
                    username = getattr(session, "username", None) if session else None
                    user_id = getattr(session, "id", None) if session else None
                except Exception:
                    username, user_id = None, None
                try:
                    with self.ctx.db.transaction() as conn:
                        conn.execute(
                            """INSERT INTO audit_log(action,entity_type,entity_id,details,user_id,username)
                               VALUES('print','invoice',?,?,?,?)""",
                            (int(invoice_id), "pos", user_id, username),
                        )
                except Exception:
                    pass
            except Exception as exc:
                # A failed print must never sound like a failed sale: the
                # invoice is already committed, and auto-print runs after
                # checkout. Use a warning so the cashier does not retry the
                # sale because of a printer/bridge problem.
                self.notify(f"تعذرت الطباعة: {exc}", kind="warning")

        if not interactive:
            try:
                layout, width = saved_print_profile(self.ctx.settings, surface="pos")
            except Exception as exc:
                print(f"nano auto-print profile failed: {exc!r}")
                return
            await print_selected(layout, width)
            return

        open_print_profile_dialog(
            self.page,
            self.ctx.settings,
            print_selected,
            surface="pos",
            native_files=self.native_files,
            preview_builder=lambda _layout, _width: receipt_html(),
        )


__all__ = ["POSCenter"]
