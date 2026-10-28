from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import flet as ft

from nano_offline.app_context import AppContext
from nano_offline.core.invoice_signing import verify_payload
from nano_offline.core import public_verify
from nano_offline.core import backup_settings
from nano_offline.core import branding
from nano_offline.core import sound
from nano_offline.core.paths import database_path, migrate_legacy_database
from nano_offline.core.toast import toast
from nano_offline.views.activation_view import ActivationGate
from nano_offline.views.admin_view import AdminCenter
from nano_offline.views.dashboard_view import DashboardCenter
from nano_offline.views.finance_view import FinanceCenter
from nano_offline.views.invoice_view import InvoiceCenter
from nano_offline.views.items_view import ItemsCenter
from nano_offline.views.login_view import LoginGate
from nano_offline.views.notifications_view import NotificationCenter
from nano_offline.views.parties_view import PartyCenter
from nano_offline.views.pos_view import POSCenter
from nano_offline.views.reports_view import ReportsCenter
from nano_offline.views.security_view import SecurityCenter
from nano_offline.views.splash_view import SplashGate
from nano_offline.views.stocktake_view import StocktakeCenter
from nano_offline.components.command_bar import CommandBar, RibbonAction, RibbonTab
from nano_offline.components.command_palette import CommandPalette
from nano_offline.core import theme
from nano_offline.core import theme_settings
from nano_offline.core.theme import Colors, Shadow
from nano_offline.core.platform_capabilities import IS_WINDOWS, supports
from nano_offline.core.platform_native_files import PlatformNativeFiles
from nano_offline.core.shell_routes import ShellNavigation, ShellRouteCoordinator, register_shell_routes

APP_DB = database_path()
LEGACY_APP_DB = next((p for p in [Path(__file__).resolve().parent.parent / "data" / "nano.db", Path(__file__).resolve().parent.parent / "data" / "qeid.db"] if p.exists()), Path(__file__).resolve().parent.parent / "data" / "nano.db")

# Optional bundled Arabic typeface. Fully offline (no external font URLs, to
# respect the app's offline-first design) — activates automatically once the
# .ttf files are placed in assets/fonts/, and is a complete no-op otherwise.
# See assets/fonts/README.md for the two files needed and where to get them.
_FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
_CUSTOM_FONTS = {
    "Plex": "fonts/IBMPlexSansArabic-Regular.ttf",
    "Plex SemiBold": "fonts/IBMPlexSansArabic-SemiBold.ttf",
}
APP_FONTS = {name: rel for name, rel in _CUSTOM_FONTS.items() if (_FONTS_DIR / Path(rel).name).exists()}
APP_FONT_FAMILY = "Plex" if "Plex" in APP_FONTS else None


def _resolve_and_set_theme(page: ft.Page, ctx: AppContext) -> str:
    """Read the stored night-mode preference/schedule and apply it to ``theme``.

    Returns the resolved mode ("light"/"dark") so callers can tell whether
    anything actually changed. ``page.platform_brightness`` is what Flet
    exposes for "what is the OS currently set to" -- wrapped in try/except
    since it isn't populated on every platform/Flet version, in which case
    "system" preference just degrades to light rather than raising.
    """
    try:
        system_dark = page.platform_brightness == ft.Brightness.DARK
    except Exception:
        system_dark = False
    effective = theme_settings.resolve_effective_mode(ctx.settings, system_is_dark=system_dark)
    theme.set_mode(effective)
    return effective


def _apply_theme(page: ft.Page) -> None:
    """Set page.theme/dark_theme with the brand seed color, current mode, and font."""
    if APP_FONTS:
        page.fonts = APP_FONTS
    page.theme = ft.Theme(color_scheme_seed=Colors.PRIMARY, font_family=APP_FONT_FAMILY)
    page.dark_theme = page.theme
    page.theme_mode = ft.ThemeMode.DARK if theme.is_dark() else ft.ThemeMode.LIGHT


def _apply_rtl_to_sheets(page: ft.Page) -> None:
    """Render every BottomSheet content RTL, like the rest of the app.

    ``page.rtl = True`` only wraps the page's own view: Flet 0.28 pushes
    modal bottom sheets as Navigator routes on top of it, so their content
    is laid out LTR unless the content control itself carries ``rtl``.
    Without this, every Arabic sheet was mirrored -- most visibly the table
    sheets, whose grid columns came out in the opposite order from the RTL
    items/invoices screens. Set it on the content right before opening so
    sheets that replace ``content`` at open time are covered too.
    """
    original_open = page.open

    def open_sheet(control):
        content = getattr(control, "content", None)
        if isinstance(control, ft.BottomSheet) and content is not None:
            content.rtl = True
        return original_open(control)

    page.open = open_sheet


def build_shell(page: ft.Page, ctx: AppContext, *, on_logout, native_files: PlatformNativeFiles, on_theme_changed):
    page.title = branding.APP_BRAND
    page.rtl = True
    _apply_theme(page)
    page.padding = 0
    page.bgcolor = Colors.BACKGROUND

    async def _auto_backup_if_overdue():
        # Best-effort, fire-and-forget, same pattern as the background
        # notification sync in main() below -- runs once per login/session
        # open (build_shell is only ever called from there), never blocks
        # the shell from rendering, and does nothing unless the admin has
        # explicitly opted in via backup_settings.auto_backup_enabled.
        try:
            if not backup_settings.auto_backup_enabled(ctx.settings):
                return
            cfg = ctx.notifications.get_config().get("backup", {})
            if not bool(cfg.get("enabled", True)):
                return
            remind_after = int(cfg.get("remind_after_days", 7))
            last_raw = ctx.notifications.settings.get("last_backup_at", "")
            if last_raw:
                days_since = (datetime.now().astimezone() - datetime.fromisoformat(last_raw).astimezone()).days
                if days_since < remind_after:
                    return
            backups_dir = ctx.db.path.parent / "backups"
            backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = backups_dir / f"auto_backup_{stamp}.nanobackup"
            await asyncio.to_thread(ctx.backup.create_backup, target)
            ctx.notifications.record_backup_completed()
            keep = backup_settings.retention_count(ctx.settings)
            if keep > 0:
                await asyncio.to_thread(ctx.backup.prune_backups, backups_dir, keep)
        except Exception:
            pass

    page.run_task(_auto_backup_if_overdue)

    content = ft.Container(
        expand=True,
        padding=ft.padding.only(left=18, right=18, top=14, bottom=18),
        opacity=1,
        animate_opacity=180,
    )

    header_title = ft.Text("الرئيسية", size=22, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY)
    header_subtitle = ft.Text("نظرة عامة على أداء عملك", size=12, color=Colors.TEXT_SECONDARY)

    def set_header(title: str, subtitle: str = "") -> None:
        header_title.value = title
        header_subtitle.value = subtitle

    def notify(text: str, kind: str | None = None):
        toast(page, text, kind=kind)

    notification_center = NotificationCenter(
        page, ctx, content, native_files=native_files, on_title_change=set_header,
        on_navigate=lambda key: navigate(key),
    )

    bell_button = ft.Container(
        ft.Stack(
            [
                ft.Container(
                    ft.Icon(ft.Icons.NOTIFICATIONS_NONE_ROUNDED, color=Colors.TEXT_SECONDARY, size=22),
                    width=42, height=42, alignment=ft.alignment.center,
                ),
                ft.Container(notification_center.badge, right=4, top=4),
            ],
        ),
        width=42,
        height=42,
        alignment=ft.alignment.center,
        border=ft.border.all(1, Colors.BORDER),
        border_radius=14,
        bgcolor=Colors.WHITE,
        shadow=Shadow.SM,
        ink=True,
        on_click=notification_center.open_panel,
    )

    # Filled after VoiceSessionController is created (see below).
    voice_header_slot = ft.Container(width=42, height=42, visible=False)

    top_bar = ft.Container(
        ft.Row(
            [
                ft.Column([header_title, header_subtitle], spacing=1, expand=True),
                voice_header_slot,
                bell_button,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        ),
        padding=ft.padding.symmetric(horizontal=18, vertical=12),
        bgcolor=Colors.WHITE,
        border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
    )

    invoice_center = InvoiceCenter(page, ctx, content, native_files=native_files, on_title_change=set_header)
    pos_center = POSCenter(
        page, ctx, content, native_files=native_files, on_title_change=set_header,
        on_create_item=lambda code: open_items_for_new(code),
        on_fullscreen_enter=lambda: pos_fullscreen_enter(),
        on_fullscreen_exit=lambda: pos_fullscreen_exit(),
    )
    # set_context_actions is defined a little further below -- wrap it in a
    # late-bound lambda so the name resolves when finance publishes its
    # ribbon actions (at render time), not here.
    finance_center = FinanceCenter(page, ctx, content, native_files=native_files, on_title_change=set_header, on_open_invoice=invoice_center.show_invoice_detail, on_context_actions=lambda actions: set_context_actions(actions))

    def open_statement(party_type: str, party_id: int | None = None) -> None:
        # Land on the Finance shell, then open the matching party
        # statement with the account preselected -- same shape as
        # open_purchase_draft above (navigate first, then drive the
        # destination center directly).
        navigate("finance")
        finance_center.show_statements(party_type, party_id)

    reports_center = ReportsCenter(
        page, ctx, content, native_files=native_files, on_title_change=set_header,
        on_open_statement=open_statement, on_open_invoice=invoice_center.show_invoice_detail,
    )
    admin_center = AdminCenter(page, ctx, content, on_logout=on_logout, native_files=native_files, on_theme_changed=on_theme_changed)
    party_center = PartyCenter(
        page, ctx, content,
        native_files=native_files,
        on_title_change=set_header,
        on_open_receipt=lambda customer_id=None, amount=None: open_receipt(customer_id, amount),
        on_open_payment=lambda supplier_id=None, amount=None: open_supplier_payment(supplier_id, amount),
        on_open_statement=open_statement,
    )
    # Lets the active view publish its own contextual ribbon actions (e.g.
    # items offers "مادة جديدة" backed by its internal editor). Guarded
    # because the command bar widget itself is built later in this function;
    # an early call just no-ops rather than crashing on the unbound name.
    def set_context_actions(actions):
        try:
            command_bar.set_actions(actions)
        except NameError:
            pass

    items_center = ItemsCenter(
        page, ctx, content, native_files=native_files, on_title_change=set_header,
        on_open_stocktake=lambda: navigate("stocktake"),
        on_context_actions=set_context_actions,
    )
    stocktake_center = StocktakeCenter(
        page, ctx, content, native_files=native_files, on_title_change=set_header,
        on_exit=lambda: navigate("items"),
    )
    security_center = SecurityCenter(page, ctx, content, on_title_change=set_header)
    # DashboardCenter needs navigate()/open_sale()/open_purchase(), which are
    # defined further below in this same function. The lambdas below only
    # resolve those names at click-time, once the whole shell is built — the
    # same late-binding the previous inline closures relied on.
    def open_purchase_draft(lines=None, supplier_id=None):
        # Navigate to invoices shell then open purchase editor with suggested lines.
        navigate("invoices")
        prefill = []
        for line in lines or []:
            prefill.append(
                {
                    "item_id": line.get("item_id"),
                    "quantity": line.get("suggested_qty") or 1,
                    "unit_price": line.get("purchase_price") or line.get("average_cost") or 0,
                    "description": line.get("name") or "",
                }
            )
        invoice_center.show_editor(
            None, "purchase", prefill_lines=prefill, party_id=supplier_id
        )

    def open_receipt(customer_id=None, amount=None):
        navigate("finance")
        seed = {"voucher_type": "receipt"}
        if customer_id is not None:
            try:
                seed["customer_id"] = int(customer_id)
            except Exception:
                pass
        if amount is not None:
            seed["amount"] = amount
        finance_center.show_vouchers()
        finance_center.show_voucher_dialog(voucher_type="receipt", initial_data=seed)

    def open_supplier_payment(supplier_id=None, amount=None):
        navigate("finance")
        seed = {"voucher_type": "payment"}
        if supplier_id is not None:
            try:
                seed["supplier_id"] = int(supplier_id)
            except Exception:
                pass
        if amount is not None:
            seed["amount"] = amount
        finance_center.show_vouchers()
        finance_center.show_voucher_dialog(voucher_type="payment", initial_data=seed)

    def open_admin_section(section: str = "backup"):
        navigate("admin")
        admin_center.show_center(section=section)

    dashboard_center = DashboardCenter(
        page, ctx, content,
        on_title_change=set_header,
        on_navigate=lambda key: navigate(key),
        on_open_sale=lambda: open_sale(),
        on_open_purchase=lambda: open_purchase(),
        on_open_notifications=notification_center.open_panel,
        on_open_purchase_draft=lambda lines, supplier_id=None: open_purchase_draft(lines, supplier_id),
        on_open_receipt=lambda customer_id=None, amount=None: open_receipt(customer_id, amount),
        on_open_admin_section=lambda section="backup": open_admin_section(section),
        native_files=native_files,
    )
    session = ctx.auth.current()
    if session is None:
        raise RuntimeError("لا توجد جلسة مستخدم")

    page_meta = {
        "dashboard": ("الرئيسية", "نظرة عامة على أداء عملك"),
        "items": ("المواد", "إدارة المخزون والخدمات والتصنيفات والوحدات"),
        "customers": ("العملاء", "إدارة العملاء والذمم المدينة"),
        "suppliers": ("الموردون", "إدارة الموردين والذمم الدائنة"),
        "invoices": ("الفواتير", "المبيعات والمشتريات وحالات السداد"),
        "finance": ("المالية", "السندات والمصروفات والحركات المالية"),
        "reports": ("التقارير", "التقارير المالية والمخزون والربحية"),
        "security": ("الأمان والدخول", "PIN والنمط والجلسات المحفوظة"),
        "admin": ("الإدارة", "المستخدمون والنسخ الاحتياطية والترخيص"),
    }

    actions = {
        "dashboard": dashboard_center.show_center,
        "items": items_center.show_center,
        "customers": lambda: party_center.show_center(ctx.customers, "العملاء"),
        "suppliers": lambda: party_center.show_center(ctx.suppliers, "الموردون"),
        "invoices": invoice_center.show_center,
        "finance": finance_center.show_center,
        "reports": reports_center.show_center,
        "security": security_center.show_center,
        "admin": admin_center.show_center,
    }

    icon_map = {
        "dashboard": ft.Icons.HOME_OUTLINED,
        "items": ft.Icons.INVENTORY_2_OUTLINED,
        "customers": ft.Icons.PEOPLE_OUTLINE,
        "suppliers": ft.Icons.LOCAL_SHIPPING_OUTLINED,
        "invoices": ft.Icons.RECEIPT_LONG_OUTLINED,
        "finance": ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
        "reports": ft.Icons.QUERY_STATS_OUTLINED,
        "security": ft.Icons.LOCK_OUTLINE,
        "admin": ft.Icons.ADMIN_PANEL_SETTINGS_OUTLINED,
    }
    label_map = {k: v[0] for k, v in page_meta.items()}
    allowed_keys = [k for k in actions if session.can(k)]
    selected_key = {"value": "dashboard"}
    sidebar_buttons: dict[str, ft.Container] = {}

    # ---------------- central Router (v0.18: real back-stack) ----------------
    # The old shell re-rendered sections from a hand-rolled navigate()
    # closure with no history at all, so the Android back button could only
    # ever "jump to dashboard" (see the old prevent_close workaround). All
    # navigation now flows through nano_offline.core.router.Router: tab taps
    # (reset) make the destination the new root, in-app transitions push,
    # and hardware back pops the stack properly. Router itself is pure
    # Python (no flet import) and unit-tested under tests/unit/test_router.py;
    # the render callback below keeps the exact cross-fade + header + badge
    # behavior the old navigate() had.
    from nano_offline.core.router import Router

    def refresh_navigation_state() -> None:
        current = selected_key["value"]
        for key, button in sidebar_buttons.items():
            active = key == current
            button.bgcolor = Colors.PRIMARY_BG if active else None
            button.border = ft.border.all(1, Colors.PRIMARY_BORDER) if active else None
            row = button.content
            if isinstance(row, ft.Row):
                for ctrl in row.controls:
                    if isinstance(ctrl, ft.Icon):
                        ctrl.color = Colors.PRIMARY if active else Colors.TEXT_SECONDARY
                    elif isinstance(ctrl, ft.Text):
                        ctrl.color = Colors.PRIMARY if active else Colors.TEXT_MUTED_DARK
        try:
            command_bar.set_active(current)
            command_bar.set_actions(_section_actions(current))
        except NameError:
            # command_bar / _section_actions are defined later in build_shell;
            # this method is only ever *called* after they exist, but guard the
            # definition-order edge just in case.
            pass

    # NOTE: adapt_navigation / _pos_fullscreen are defined further below,
    # next to the voice-session block they depend on. They are only *called*
    # from render_route / pos_chrome_restore closures, i.e. at click time,
    # once the whole shell (including them) exists.

    route_coordinator = ShellRouteCoordinator(
        active_key=lambda key: selected_key.__setitem__("value", key),
        keyboard_handler=lambda: _shell_key,
        set_header=set_header,
        refresh_navigation=refresh_navigation_state,
        notify=notify,
        refresh_badge=notification_center.refresh_badge,
    )

    def render_sale(_=None) -> None:
        invoice_center.show_editor(None, "sale")

    def render_purchase(_=None) -> None:
        invoice_center.show_editor(None, "purchase")

    def pos_chrome_restore(_key: str = "", _kwargs: dict | None = None) -> None:
        # Registered as the POS route's on_leave: any way out of fullscreen
        # POS (tab tap, hardware back, POS's own exit button) restores the
        # shell chrome exactly once, before the next screen renders.
        _pos_fullscreen["value"] = False
        top_bar.visible = True
        try:
            if voice_session is not None:
                voice_header_slot.visible = True
        except Exception:
            pass
        adapt_navigation()

    router = Router(
        home="dashboard",
        render=lambda key, kwargs: route_coordinator.render(router, page, content, key, kwargs),
        can_render=lambda route: session.can(route.permission or route.key),
        on_denied=route_coordinator.on_denied,
    )
    register_shell_routes(
        router,
        page_meta=page_meta,
        actions=actions,
        icon_map=icon_map,
        pos_render=pos_center.show_center,
        sale_render=render_sale,
        purchase_render=render_purchase,
        stocktake_render=stocktake_center.show_center,
        pos_on_leave=pos_chrome_restore,
    )

    shell_navigation = ShellNavigation(
        router,
        set(actions),
        open_notifications=notification_center.open_panel,
        on_notification_error=lambda exc: notify(str(exc), kind="error"),
    )
    navigate = shell_navigation.navigate

    # Thin wrappers kept for the existing call-sites (sidebar buttons, the
    # ribbon actions, dashboard quick actions). All of them delegate to the
    # Router instead of re-implementing the opacity/header dance themselves.
    def open_pos(_=None) -> None:
        navigate("pos")

    def open_sale(_=None) -> None:
        navigate("sale")

    def open_purchase(_=None) -> None:
        navigate("purchase")

    def open_items_for_new(barcode_code: str) -> None:
        navigate("items", prefill_barcode=barcode_code)

    # ---------------- desktop ribbon (replaces the old sidebar) ----------------
    def _section_actions(key: str) -> list[RibbonAction]:
        """Default contextual ribbon actions per section.

        A view may override these by calling ``set_context_actions`` while it
        renders (the items screen does, to expose its own editor). These are
        the shell-level fallbacks for every other section.
        """
        invoice_actions = [
            RibbonAction("sale", "فاتورة بيع", ft.Icons.SHOPPING_CART_CHECKOUT, on_click=lambda _=None: open_sale()),
            RibbonAction("purchase", "فاتورة شراء", ft.Icons.ADD_SHOPPING_CART, on_click=lambda _=None: open_purchase()),
            RibbonAction("verify", "تحقق من فاتورة", ft.Icons.QR_CODE_SCANNER_OUTLINED, on_click=lambda _=None: open_verify_invoice()),
        ]
        table_invoice_action = RibbonAction(
            "table_invoice", "فاتورة شاملة (جدول)", ft.Icons.TABLE_VIEW_ROUNDED,
            on_click=lambda _=None: invoice_center.show_table_invoice_sheet(),
        )
        if key == "dashboard":
            # POS is a dashboard-first surface: keep its ribbon entry only
            # here, not on the invoices screen where it duplicated the
            # sidebar shortcut and crowded the invoice actions.
            return [
                RibbonAction("pos", "نقطة البيع", ft.Icons.POINT_OF_SALE, on_click=lambda _=None: open_pos()),
            ] + invoice_actions
        if key in ("invoices", "sale", "purchase"):
            # The comprehensive table entry must survive the jump from the
            # invoices screen into the sale/purchase editors -- otherwise it
            # vanishes exactly when the user is mid-invoice and most likely
            # to want it.
            return invoice_actions + [table_invoice_action]
        if key in ("items", "pos"):
            return invoice_actions
        if key == "finance":
            return [
                RibbonAction("receipt", "سند قبض", ft.Icons.PAYMENTS_OUTLINED, on_click=lambda _=None: open_receipt()),
                RibbonAction("payment", "سند صرف", ft.Icons.REQUEST_QUOTE_OUTLINED, on_click=lambda _=None: open_supplier_payment()),
            ]
        if key == "admin":
            return [RibbonAction("backup", "النسخ الاحتياطي", ft.Icons.BACKUP_OUTLINED, on_click=lambda _=None: open_admin_section("backup"))]
        return []

    session_for_bar = session
    command_bar = CommandBar(
        page=page,
        tabs=[RibbonTab(k, label_map[k], icon_map[k]) for k in allowed_keys],
        on_tab=lambda k: navigate(k),
        user_name=session_for_bar.full_name or session_for_bar.username,
        user_sub=session_for_bar.username,
        on_logout=lambda: on_logout(),
        on_search=None,
    )
    command_bar.visible = False

    # The window's close (X) button closes the app normally -- the Android
    # hardware-back interception (prevent_close + os._exit) is gone with the
    # mobile shell. In-app navigation uses the Router directly.

    def nav_button(key: str, label: str, icon, on_click):
        icon_ctrl = ft.Icon(icon, size=21, color=Colors.TEXT_SECONDARY)
        text_ctrl = ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=Colors.TEXT_MUTED_DARK, expand=True)
        box = ft.Container(
            ft.Row([icon_ctrl, text_ctrl, ft.Icon(ft.Icons.CHEVRON_LEFT, size=15, color=Colors.BORDER_STRONG)], spacing=12),
            padding=ft.padding.symmetric(horizontal=14, vertical=12),
            border_radius=14,
            on_click=on_click,
            ink=True,
        )
        if key in actions:
            sidebar_buttons[key] = box
        return box

    sidebar_controls: list[ft.Control] = []
    for key in allowed_keys:
        sidebar_controls.append(nav_button(key, label_map[key], icon_map[key], lambda _, k=key: navigate(k)))
        if key == "items" and session.can("invoices"):
            sidebar_controls.append(nav_button("pos", "نقطة البيع", ft.Icons.POINT_OF_SALE, open_pos))
            sidebar_controls.append(nav_button("sale", "فاتورة بيع", ft.Icons.SHOPPING_CART_CHECKOUT, open_sale))
            sidebar_controls.append(nav_button("purchase", "فاتورة شراء", ft.Icons.ADD_SHOPPING_CART, open_purchase))

    avatar_text = "".join(part[:1] for part in (session.full_name or session.username).split()[:2]).upper() or "N"
    sidebar = ft.Container(
        ft.Column(
            [
                ft.Container(
                    ft.Row(
                        [
                            ft.Image(src="icon.png", width=48, height=48, fit=ft.ImageFit.CONTAIN),
                            ft.Column([ft.Text(branding.APP_BRAND, size=19, weight=ft.FontWeight.BOLD, color=Colors.TEXT_PRIMARY), ft.Text(branding.APP_TAGLINE, size=10, color=Colors.TEXT_SECONDARY)], spacing=1, expand=True),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.padding.only(left=18, right=18, top=18, bottom=14),
                    border=ft.border.only(bottom=ft.BorderSide(1, Colors.BORDER)),
                ),
                ft.Container(ft.Column(sidebar_controls, spacing=5, scroll=ft.ScrollMode.AUTO), padding=12, expand=True),
                ft.Container(
                    ft.Row(
                        [
                            ft.Container(ft.Text(avatar_text, color=Colors.WHITE, weight=ft.FontWeight.BOLD), width=40, height=40, alignment=ft.alignment.center, bgcolor=Colors.PRIMARY, border_radius=14),
                            ft.Column([ft.Text(session.full_name, size=12, weight=ft.FontWeight.BOLD), ft.Text(session.username, size=10, color=Colors.TEXT_SECONDARY)], spacing=1, expand=True),
                            ft.IconButton(icon=ft.Icons.LOGOUT, tooltip="تسجيل الخروج", icon_color=Colors.TEXT_SECONDARY, on_click=lambda _: on_logout()),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=12,
                    border=ft.border.only(top=ft.BorderSide(1, Colors.BORDER)),
                ),
            ],
            spacing=0,
            expand=True,
        ),
        width=280,
        bgcolor=Colors.WHITE,
        border=ft.border.only(left=ft.BorderSide(1, Colors.BORDER)),
        shadow=Shadow.LG,
        visible=False,
    )

    # The desktop ribbon sits above the section header; the old branded
    # sidebar is no longer used -- the ribbon is the one navigation surface.
    body = ft.Column([command_bar, top_bar, content], spacing=0, expand=True)

    def _show_verify_result(is_valid: bool, reason: str) -> None:
        # Same dismiss-it-yourself AlertDialog pattern used elsewhere in the
        # app (see admin_view._notify_error) -- a tamper warning must not be
        # missable behind an auto-dismissing SnackBar.
        result_dialog = ft.AlertDialog(
            modal=True,
            icon=ft.Icon(
                ft.Icons.CHECK_CIRCLE if is_valid else ft.Icons.ERROR_OUTLINE,
                color=Colors.SUCCESS if is_valid else Colors.DANGER,
                size=42,
            ),
            title=ft.Text(
                "الفاتورة أصلية" if is_valid else "تحذير تلاعب",
                text_align=ft.TextAlign.CENTER,
            ),
            content=ft.Text(reason, text_align=ft.TextAlign.CENTER),
            actions=[ft.FilledButton("حسنًا", on_click=lambda _: page.close(result_dialog))],
            actions_alignment=ft.MainAxisAlignment.CENTER,
        )
        page.open(result_dialog)

    async def _verify_invoice_scan() -> None:
        # Hands off to the native camera scanner right after the verify sheet
        # is closed. A brief pause lets the sheet's close animation land
        # before the native scanner activity steals focus; otherwise the
        # close update can be dropped and the sheet still looks open on
        # return, forcing the user to tap again.
        await asyncio.sleep(0.25)
        try:
            payload = await native_files.scan_barcode()
        except RuntimeError as e:
            toast(page, str(e))
            return
        if not payload:
            return  # user backed out of the scanner -- not an error
        is_valid, reason = verify_payload(ctx.db, payload)
        _show_verify_result(is_valid, reason)

    def _manual_verify_dialog() -> None:
        inv_field = ft.TextField(
            label="رقم الفاتورة (كما على الورقة)",
            hint_text="مثال: 12 أو INV-00012",
            width=320, dense=True,
        )
        token_field = ft.TextField(
            label="رمز التحقق (من أسفل الورقة)",
            hint_text="6 أحرف — مثال: 4K7M2Q",
            width=320, dense=True, max_length=12,
        )

        def run_manual(_) -> None:
            ok_manual, reason_manual, _invoice_row = public_verify.verify_manual(
                ctx.db, inv_field.value or "", token_field.value or ""
            )
            page.close(manual_dialog)
            _show_verify_result(ok_manual, reason_manual)

        manual_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("تحقق يدوي من الفاتورة", text_align=ft.TextAlign.CENTER),
            content=ft.Column(
                [inv_field, token_field],
                tight=True, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            actions=[
                ft.FilledButton("تحقق", on_click=run_manual),
                ft.TextButton("إلغاء", on_click=lambda _: page.close(manual_dialog)),
            ],
            actions_alignment=ft.MainAxisAlignment.CENTER,
        )
        page.open(manual_dialog)

    def open_verify_invoice(_=None) -> None:
        # Two ways to check a printed invoice: scan its QR with the camera,
        # or type the printed number + the 6-character public token (which
        # works even when the device has no camera/scanner at hand). Both
        # verify fully offline against this database -- no network needed.
        verify_sheet = ft.BottomSheet(
            ft.Container(
                ft.Column(
                    [
                        ft.Container(width=44, height=5, bgcolor=Colors.BORDER_STRONG, border_radius=10, alignment=ft.alignment.center),
                        ft.Text("التحقق من فاتورة", size=20, weight=ft.FontWeight.BOLD),
                        ft.Text("تأكد من أن الأرقام المطبوعة لم تُعدَّل بعد الطباعة", size=11, color=Colors.TEXT_SECONDARY),
                        *(
                            [
                                ft.Container(
                                    ft.Row(
                                        [
                                            ft.Container(ft.Icon(ft.Icons.QR_CODE_SCANNER_OUTLINED, color=Colors.PRIMARY, size=24), width=46, height=46, alignment=ft.alignment.center, bgcolor=Colors.PRIMARY_BG, border_radius=15),
                                            ft.Column([ft.Text("مسح رمز QR بالكاميرا", size=14, weight=ft.FontWeight.W_600), ft.Text("يفتح الكاميرا ويقرأ رمز الفاتورة", size=11, color=Colors.TEXT_SECONDARY)], spacing=2, expand=True),
                                            ft.Icon(ft.Icons.CHEVRON_LEFT, size=16, color=Colors.BORDER_STRONG),
                                        ],
                                        spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    padding=ft.padding.symmetric(horizontal=14, vertical=12),
                                    border_radius=14, ink=True,
                                    on_click=lambda _: (page.close(verify_sheet), page.run_task(_verify_invoice_scan)),
                                )
                            ]
                            if supports("barcode_camera_scan")
                            else []
                        ),
                        ft.Container(
                            ft.Row(
                                [
                                    ft.Container(ft.Icon(ft.Icons.KEYBOARD_OUTLINED, color=Colors.PURPLE, size=24), width=46, height=46, alignment=ft.alignment.center, bgcolor=Colors.PURPLE_BG, border_radius=15),
                                    ft.Column([ft.Text("إدخال يدوي", size=14, weight=ft.FontWeight.W_600), ft.Text("رقم الفاتورة + رمز التحقق المطبوع", size=11, color=Colors.TEXT_SECONDARY)], spacing=2, expand=True),
                                    ft.Icon(ft.Icons.CHEVRON_LEFT, size=16, color=Colors.BORDER_STRONG),
                                ],
                                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=ft.padding.symmetric(horizontal=14, vertical=12),
                            border_radius=14, ink=True,
                            on_click=lambda _: (page.close(verify_sheet), _manual_verify_dialog()),
                        ),
                    ],
                    spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.only(left=18, right=18, top=12, bottom=24),
                bgcolor=Colors.WHITE,
                border_radius=ft.border_radius.only(top_left=28, top_right=28),
                shadow=Shadow.LG,
            )
        )
        page.open(verify_sheet)

    # The shell is desktop-only now: one ribbon navigation surface (CommandBar)
    # above the section header -- no bottom bar / floating sale button.
    root = ft.Row([sidebar, body], expand=True, spacing=0)
    page.add(ft.SafeArea(root, expand=True))

    # In-app voice «call» session — survives section switches, not a true
    # OS background service. Floating banner + FAB hang over the shell.
    voice_session = None
    if supports("speech_recognition"):
        try:
            from nano_offline.core.voice_session import VoiceSessionController
            voice_session = VoiceSessionController(
                page,
                ctx,
                navigate=navigate,
                notify=lambda t, **kw: toast(page, t, **kw),
                get_section=lambda: selected_key.get("value") or "dashboard",
                native_files=native_files,
                tts_enabled=True,
            )
            # Floating draggable bubble (positioned by VoiceSessionController)
            page.overlay.append(voice_session.banner)
            # Call control lives in the top bar so it never covers the content.
            voice_header_slot.content = voice_session.header_btn
            voice_header_slot.visible = True
            page.floating_action_button = None
            ctx._voice_session = voice_session  # type: ignore[attr-defined]
            page.update()
        except Exception:
            voice_session = None

    _pos_fullscreen = {"value": False}

    def adapt_navigation(_=None):
        if _pos_fullscreen["value"]:
            # A window resize mid-sale must not pop the ribbon back over the
            # sale screen -- leaving POS is the Router's on_leave path
            # (pos_chrome_restore below), which clears this flag first. Hand
            # the resize to POS (via the hook POSCenter._build installs) so
            # the sale screen can still reflow instead of staying stuck.
            hook = getattr(page, "_nano_pos_relayout", None)
            if callable(hook):
                try:
                    hook()
                except Exception:
                    pass
            return
        # Desktop-only: the ribbon is always the navigation surface.
        sidebar.visible = False
        command_bar.visible = True
        content.padding = ft.padding.only(left=24, right=24, top=18, bottom=24)
        page.update()

    # POS fullscreen: hides *every* piece of the app's own chrome (top header,
    # ribbon) so the sale screen owns the whole viewport edge-to-edge,
    # kiosk-style. adapt_navigation() re-derives the chrome, so exiting just
    # re-runs it instead of duplicating that logic here.
    def pos_fullscreen_enter():
        _pos_fullscreen["value"] = True
        top_bar.visible = False
        sidebar.visible = False
        command_bar.visible = False
        content.padding = ft.padding.all(0)
        # Keep the call banner; hide the header control so it does not cover
        # the POS pay bar.
        try:
            voice_header_slot.visible = False
        except Exception:
            pass
        page.update()

    def pos_fullscreen_exit():
        # Leaving POS is a navigation: routing out fires the POS route's
        # on_leave hook (pos_chrome_restore), which clears the fullscreen
        # flag and re-derives the chrome for the current width. So here we
        # only pick the destination -- back to where POS was opened from if
        # there's history, otherwise home.
        if router.can_go_back():
            router.back()
        else:
            navigate("dashboard")

    # ---------------- Ctrl+K command palette + global shortcuts ----------------
    command_palette = CommandPalette(
        page,
        [(key, label_map[key], icon_map[key]) for key in allowed_keys],
        on_select=navigate,
    )
    open_command_palette = command_palette.open

    def _shell_key(e: ft.KeyboardEvent) -> None:
        norm = (e.key or "").strip().lower().replace(" ", "")
        if command_palette.handle_key(norm):
            return
        if getattr(e, "ctrl", False) and norm == "k":
            open_command_palette()

    # Expose the palette to the DataGrid (it reads this off the page so its
    # own key handler can honour Ctrl+K without importing the shell).
    try:
        page._nano_open_command_palette = open_command_palette
    except Exception:
        pass

    command_bar.on_search = open_command_palette
    page.on_keyboard_event = _shell_key

    page.on_resize = adapt_navigation
    navigate("dashboard")
    adapt_navigation()



def main(page: ft.Page):
    from nano_offline.core.logging_config import get_logger, setup_logging

    setup_logging()
    _log = get_logger("nano.startup")
    # Voice command hook: default is StubVoiceEngine (UI works, no STT yet).
    # To plug a real on-device Arabic model or platform STT:
    #   from nano_offline.core.voice_command import set_engine, YourEngine
    #   set_engine(YourEngine(...))
    migrate_legacy_database(LEGACY_APP_DB, APP_DB)
    # Local (default) or client mode. Client mode routes the shared business
    # data to a central LAN server (services.hybrid_context.context_for); it is
    # off unless this device opted in, so local behaviour is unchanged when off.
    # Never blocks startup on failure.
    try:
        from nano_offline.core.local_prefs import LocalPrefs
        from nano_offline.services.hybrid_context import context_for

        ctx = context_for(APP_DB, LocalPrefs(APP_DB.parent / "local_prefs.json"))
    except Exception:
        _log.warning("client-mode setup failed; using local context", exc_info=True)
        ctx = AppContext.create(APP_DB)
    # If client mode was requested but the server was unreachable, context_for
    # fell back to local and left a reason here to surface after startup.
    _client_fallback = None
    try:
        from nano_offline.services import hybrid_context as _hybrid_ctx

        _client_fallback = getattr(_hybrid_ctx, "LAST_FALLBACK", None)
    except Exception:
        _client_fallback = None
    native_files = PlatformNativeFiles()
    page.overlay.append(native_files)

    # Built-in LAN price-check server for customer screens: started here so
    # it shares the app's data/settings. Best-effort and a no-op unless the
    # admin enabled it in the "شاشة الأسعار" tab; never blocks app startup.
    try:
        from nano_offline.services import lan_price_server

        lan_price_server.start_if_enabled(ctx)
    except Exception:
        _log.warning("LAN price server startup failed", exc_info=True)

    # Read-only LAN API server (client/server data plane, phase 1): same
    # best-effort, gated by its own admin setting, never blocks startup.
    try:
        from nano_offline.services import lan_api_server

        lan_api_server.start_if_enabled(ctx)
    except Exception:
        _log.warning("LAN API server startup failed", exc_info=True)

    # Voice: prefer Android on-device SpeechRecognizer when available.
    try:
        from nano_offline.core import voice_command as _voice
        plat = str(getattr(page, "platform", "") or "").lower()
        if plat in ("android", "android_phone", "android_tablet") or "android" in plat:
            _voice.set_engine(_voice.AndroidNativeVoiceEngine(native_files, page=page))
    except Exception:
        pass
    # Lets core/sound.py's play() -- invoked from inside toast(), which only
    # ever receives `page` -- read sound settings and reach the native
    # AudioPool bridge (native_files.play_sound) without threading them
    # through every one of the ~50 existing toast()/notify() call sites.
    # Set once here, before login/activation screens even build, so every
    # toast in the app (including pre-login ones) gets sound from the start.
    # Must come after `native_files` exists -- attach_context() stores that
    # exact instance, and it needs to already be mounted in page.overlay
    # for invoke_method_async to reach anything on the Dart side.
    sound.attach_context(page, ctx, native_files)

    async def _preload_sound_pools():
        # Best-effort: warms up the four native AudioPools (see
        # extensions/flet_native_files' sound_pool.dart) so the very first
        # toast of the session doesn't pay the one-time asset-decode delay
        # before it can play its tone. Never blocks or fails app startup --
        # sound.play() itself triggers the same load lazily if this hasn't
        # finished yet by the time the first toast fires.
        try:
            await native_files.init_sound()
        except Exception:
            pass

    page.run_task(_preload_sound_pools)

    async def _sync_background_notifications():
        # Best-effort: registers/refreshes the Android WorkManager check that
        # can alert the user while Nano itself isn't running (see
        # NotificationService.native_schedule_payload and
        # flet_native_files' schedule_notifications). Never blocks or fails
        # app startup -- the in-app bell/alerts already work independently
        # of this, on iOS/desktop/web this call is just a no-op.
        try:
            await native_files.schedule_notifications(**ctx.notifications.native_schedule_payload())
        except Exception:
            pass
        # PHASE9 fix: on Android 13+ a notification is silently dropped
        # unless the POST_NOTIFICATIONS runtime permission was granted. The
        # settings screen asks for it, but a user who never opens that
        # screen got no external notifications at all -- so request it here
        # too, right after the background check is registered, so the
        # system prompt appears on first launch and closed-app alerts can
        # actually be delivered. Best-effort like the rest of this task.
        try:
            await native_files.request_notification_permission()
        except Exception:
            pass

    page.run_task(_sync_background_notifications)

    async def _dispatch_external_alerts():
        # PHASE11: fan out any pending smart alerts through the external
        # channels the admin enabled (Telegram / email / webhook) on
        # startup, so a condition first detected while the app was closed
        # still reaches the owner. Best-effort -- external delivery must
        # never block or break app startup.
        try:
            await ctx.external_notifications.dispatch_async()
        except Exception:
            pass

    page.run_task(_dispatch_external_alerts)

    # Windows system notifications (app-open only): the rules engine calls
    # hooks from its own worker thread, so capture the Flet event loop here
    # and hop back onto it for each alert. Android keeps using WorkManager
    # (closed-app) + flutter_local_notifications; there is no desktop
    # equivalent for a closed app, which is why background_notifications is
    # reported as off:app-open-only in platform_capabilities.
    desktop_alert_loop_started = {"value": False}
    desktop_shell_active = {"value": False}
    desktop_system_dispatch = {"fn": None}
    if IS_WINDOWS:
        async def _show_desktop_alert(alert):
            try:
                await native_files.show_system_notification(title=alert.title, body=alert.body)
            except Exception:
                pass

        async def _register_desktop_notifications():
            loop = asyncio.get_running_loop()

            def _dispatch(alerts):
                for alert in alerts:
                    try:
                        asyncio.run_coroutine_threadsafe(_show_desktop_alert(alert), loop)
                    except Exception:
                        pass

            # Store only; open_shell arms it after a successful login so no
            # business toast can appear on the splash/activation/login gates.
            # The flag covers the rare case where the login gate is already
            # gone by the time this task first runs.
            desktop_system_dispatch["fn"] = _dispatch
            if desktop_shell_active["value"]:
                ctx.notifications.set_system_hook(_dispatch)

        page.run_task(_register_desktop_notifications)

        async def _desktop_alert_loop():
            # The in-app engine normally runs on navigation. While Nano is
            # open (possibly minimized) on Windows, refresh it every five
            # minutes so a condition that appears after login still reaches
            # the Action Center without the user clicking anything.
            while True:
                await asyncio.sleep(300)
                if not desktop_shell_active["value"]:
                    continue
                try:
                    # Same throttled/daemon-thread path navigation uses, so
                    # this can never race a navigation-triggered pass on
                    # the SQLite transaction.
                    ctx.notifications.sync_in_background(min_interval=0.0)
                except Exception:
                    pass

    page.title = branding.APP_BRAND
    page.rtl = True
    _apply_rtl_to_sheets(page)
    _resolve_and_set_theme(page, ctx)
    _apply_theme(page)
    page.padding = 0
    page.bgcolor = Colors.BACKGROUND

    # Which top-level screen is currently mounted -- only "shell" needs a
    # full teardown/rebuild when the theme changes mid-session (the other
    # screens are gates that already call reset_page(), which re-syncs the
    # theme, on their own next transition).
    current_screen = {"value": "splash"}

    def reset_page():
        _resolve_and_set_theme(page, ctx)
        _apply_theme(page)
        page.controls.clear()
        page.navigation_bar = None
        page.update()

    def open_shell():
        current_screen["value"] = "shell"
        if IS_WINDOWS:
            # Re-arm desktop toasts after a logout cleared them, and start
            # the periodic refresh only once the user is past activation/
            # login -- no business alert should pop up on the splash or
            # login screen. Theme rebuilds call open_shell again; the flag
            # keeps them from stacking a second loop.
            desktop_shell_active["value"] = True
            if desktop_system_dispatch["fn"] is not None:
                ctx.notifications.set_system_hook(desktop_system_dispatch["fn"])
            if not desktop_alert_loop_started["value"]:
                desktop_alert_loop_started["value"] = True
                page.run_task(_desktop_alert_loop)
        reset_page()
        try:
            build_shell(page, ctx, on_logout=logout, native_files=native_files, on_theme_changed=open_shell)
        except Exception as exc:
            # Never leave the user on a blank/gray page: build_shell failing
            # used to be silent (controls already cleared by reset_page).
            # Surface the real cause, keep a retry, and record it in the
            # on-device log + crash log (not just stderr).
            _log.exception("build_shell failed: %s", exc)

            def retry(_=None):
                page.close(err_dialog)
                open_shell()

            err_dialog = ft.AlertDialog(
                modal=True,
                icon=ft.Icon(ft.Icons.ERROR_OUTLINE, color=Colors.DANGER, size=40),
                title=ft.Text("تعذر تحميل الواجهة", text_align=ft.TextAlign.CENTER),
                content=ft.Column(
                    [
                        ft.Text(str(exc) or exc.__class__.__name__, selectable=True),
                        ft.Text("جرّب إعادة التشغيل. إن تكررت المشكلة أبلغ الدعم بهذه الرسالة.", size=11, color=Colors.TEXT_SECONDARY),
                    ],
                    tight=True,
                    width=320,
                ),
                actions=[ft.FilledButton("إعادة المحاولة", on_click=retry)],
                actions_alignment=ft.MainAxisAlignment.CENTER,
            )
            page.open(err_dialog)
            page.update()

    def handle_brightness_change(_=None):
        # Best-effort: only matters at all for users on "تلقائي حسب النظام"
        # (system) with no active dark schedule -- resolve_effective_mode()
        # already ignores this for an explicit light/dark preference or a
        # schedule window in effect. Only pay for a full shell rebuild if
        # the resolved mode actually flipped.
        before = theme.get_mode()
        after = _resolve_and_set_theme(page, ctx)
        if after == before:
            return
        _apply_theme(page)
        if current_screen["value"] == "shell":
            open_shell()
        else:
            page.update()

    try:
        page.on_platform_brightness_change = handle_brightness_change
    except Exception:
        pass

    def logout():
        ctx.auth.logout()
        show_auth()

    def notify(text: str):
        toast(page, text)

    def show_activation():
        current_screen["value"] = "activation"
        if IS_WINDOWS:
            desktop_shell_active["value"] = False
            ctx.notifications.set_system_hook(None)
        reset_page()
        ActivationGate(page, ctx, on_success=show_auth).show()

    def show_auth():
        current_screen["value"] = "auth"
        if IS_WINDOWS:
            desktop_shell_active["value"] = False
            ctx.notifications.set_system_hook(None)
        reset_page()
        LoginGate(page, ctx, on_success=open_shell).show()

    def route_after_splash():
        # Decided here, once the splash's minimum on-screen beat has
        # elapsed, rather than before showing it -- so the very first
        # thing painted is always the branded splash, and *then* it hands
        # off to whichever of activation/login/shell actually applies,
        # never the other way around.
        reset_page()
        if ctx.license.status().valid:
            if ctx.auth.has_users() and ctx.auth.restore_saved_session() is not None:
                open_shell()
            else:
                show_auth()
        else:
            show_activation()

    SplashGate(page, on_ready=route_after_splash).show()

    if _client_fallback:
        async def _show_client_fallback():
            try:
                await asyncio.sleep(1.2)
                toast(page, _client_fallback, kind="warning")
            except Exception:
                pass

        page.run_task(_show_client_fallback)


# NOTE: keep this call unconditional and module-level. The Android Flet
# runtime imports this file as a regular module (not __main__) and relies
# on the embedded ft.app() call to start the app -- a __main__ guard here
# ships an APK that starts and then renders nothing (gray screen).
ft.app(target=main)
