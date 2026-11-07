"""Headless build-smoke for every major screen.

Constructs each view center against a real (temporary) AppContext and calls
its ``show_center()``, asserting it builds a control tree without raising.
This is the layer pytest was missing: it catches construct/parameter/layout
regressions (the kind that caused the recent tab-cache and button bugs)
without a running Flet app.

Fixture-free so it also runs under the standalone runner.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import flet as ft

from nano_offline.app_context import AppContext


class FakePage:
    """Minimal stand-in for ``ft.Page`` -- only what a screen touches."""

    def __init__(self) -> None:
        self.overlay: list = []
        self.width = 1280
        self.height = 800
        self.padding = None
        self.bgcolor = None
        self.rtl = True
        self.theme = None
        self.dark_theme = None
        self.theme_mode = None
        self.fonts = None
        self.title = ""
        self.on_keyboard_event = None
        self.on_resize = None
        self.updated = 0
        self.opened: list = []

    def update(self, *args, **kwargs):
        self.updated += 1

    def open(self, control):
        self.opened.append(control)
        try:
            control.open = True
        except Exception:
            pass

    @staticmethod
    def close(control):
        try:
            control.open = False
        except Exception:
            pass

    def run_task(self, handler, *args, **kwargs):
        # Execute nothing, but drain a returned coroutine so Python never warns
        # about an un-awaited async call made during a screen build.
        try:
            result = handler(*args, **kwargs)
        except Exception:
            return
        close = getattr(result, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def run_thread(self, handler, *args, **kwargs):
        try:
            handler(*args, **kwargs)
        except Exception:
            pass

    def add(self, *controls, **kwargs):
        pass

    def scroll_to(self, **kwargs):
        pass

    def clean(self):
        pass


def _ctx() -> tuple[AppContext, str]:
    tmp = tempfile.mkdtemp(prefix="nano-ui-")
    ctx = AppContext.create(Path(tmp) / "nano.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    return ctx, tmp


def _assert_built(center, content):
    center.show_center()
    assert content.content is not None, "screen built no content"


def test_dashboard_and_items_screens_build():
    from nano_offline.views.items_view import ItemsCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = ItemsCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_invoice_screen_builds():
    from nano_offline.views.invoice_view import InvoiceCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = InvoiceCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_finance_screen_builds():
    from nano_offline.views.finance_view import FinanceCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = FinanceCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reports_screen_builds():
    from nano_offline.views.reports_view import ReportsCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = ReportsCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_parties_screens_build():
    from nano_offline.views.parties_view import PartyCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = PartyCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        center.show_center(ctx.customers, "العملاء")
        assert content.content is not None
        content.content = None
        center.show_center(ctx.suppliers, "الموردون")
        assert content.content is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_security_screen_builds():
    from nano_offline.views.security_view import SecurityCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = SecurityCenter(page, ctx, content, on_title_change=lambda *a: None)
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_admin_screen_builds():
    from nano_offline.views.admin_view import AdminCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = AdminCenter(
            page, ctx, content, on_logout=lambda: None, native_files=None,
            on_theme_changed=lambda: None, on_context_actions=lambda *a: None,
        )
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pos_screen_builds():
    from nano_offline.views.pos_view import POSCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = POSCenter(
            page, ctx, content, native_files=None, on_title_change=lambda *a: None,
            on_create_item=lambda *a: None, on_fullscreen_enter=lambda: None,
            on_fullscreen_exit=lambda: None, on_toggle_fullscreen=lambda: True,
        )
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stocktake_screen_builds():
    from nano_offline.views.stocktake_view import StocktakeCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = StocktakeCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        _assert_built(center, content)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _last_opened_sheet(page):
    assert page.opened, "no sheet was opened"
    return page.opened[-1]


def test_invoice_table_sheet_builds_for_sale_and_purchase():
    from nano_offline.views.invoice_view import InvoiceCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = InvoiceCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        for kind in ("sale", "purchase"):
            center.show_table_invoice_sheet(kind)
            sheet = _last_opened_sheet(page)
            assert getattr(sheet, "content", None) is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_new_invoice_paged_sheet_builds():
    from nano_offline.views.invoice_view import InvoiceCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = InvoiceCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        center.show_new_invoice_sheet("sale")
        sheet = _last_opened_sheet(page)
        assert getattr(sheet, "content", None) is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_invoice_editor_builds_for_existing_invoice():
    from nano_offline.views.invoice_view import InvoiceCenter
    from nano_offline.services.invoice_service import InvoiceLineInput

    ctx, tmp = _ctx()
    try:
        unit = ctx.definitions.create_unit("قطعة", "ق")
        customer = ctx.customers.create("عميل")
        item = ctx.items.create(name="مادة", purchase_price=2.0, selling_price=3.0, quantity=10.0, base_unit_id=unit, barcode="UIE-1")
        ctx.invoices.convert_opening_balances(item_ids=[item], reference="أول المدة")
        sale = ctx.invoices.create_invoice(
            invoice_type="sale", customer_id=customer, paid_amount=0,
            lines=[InvoiceLineInput(description="x", item_id=item, quantity=1, unit_price=3.0, unit_id=unit)],
        )

        page, content = FakePage(), ft.Container(expand=True)
        center = InvoiceCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        center.show_editor(sale, "sale")
        sheet = _last_opened_sheet(page)
        assert getattr(sheet, "content", None) is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_items_bulk_sheet_builds_for_add_and_edit():
    from nano_offline.views.items_view import ItemsCenter

    ctx, tmp = _ctx()
    try:
        page, content = FakePage(), ft.Container(expand=True)
        center = ItemsCenter(page, ctx, content, native_files=None, on_title_change=lambda *a: None)
        center.show_center()

        center.show_bulk_sheet()
        assert page.opened and getattr(page.opened[-1], "content", None) is not None

        unit = ctx.definitions.create_unit("كيلو", "كغ")
        item = ctx.items.create(
            name="مادة", purchase_price=2.0, selling_price=3.0, quantity=0.0,
            base_unit_id=unit, barcode="BULK-1",
        )
        center.show_bulk_sheet(edit_item=ctx.items.get(item))
        assert getattr(page.opened[-1], "content", None) is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
