"""The payment dialog must use the invoice's own currency snapshot.

Reproduces the reported bug: create a SYP invoice, switch the app's
display currency to USD, then open «تسجيل دفعة» -- the amount field used
to be seeded in dollars (10) even though the invoice is in pounds
(135,000).
"""

from __future__ import annotations

import flet as ft

from nano_offline.app_context import AppContext
from nano_offline.components import SmartAmountField
from nano_offline.core import currency
from nano_offline.services.invoice_service import InvoiceLineInput
from nano_offline.views.invoice_view import InvoiceCenter


class _FakePage:
    width = 1280
    height = 800
    rtl = True
    on_keyboard_event = None

    def __init__(self) -> None:
        self.opened: list = []
        self.overlay: list = []

    def open(self, control) -> None:  # noqa: ANN001
        self.opened.append(control)

    def close(self, control) -> None:  # noqa: ANN001
        pass

    def update(self) -> None:
        pass

    def run_task(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass


def _find_amount_field(control) -> SmartAmountField | None:  # noqa: ANN001
    if isinstance(control, SmartAmountField):
        return control
    for attr in ("controls", "content"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            for child in value:
                found = _find_amount_field(child)
                if found is not None:
                    return found
        elif value is not None:
            found = _find_amount_field(value)
            if found is not None:
                return found
    return None


def test_payment_dialog_keeps_syp_amount_after_display_currency_switch(tmp_path):
    ctx = AppContext.create(str(tmp_path / "payment.db"))
    ctx.settings.set(currency.DISPLAY_CURRENCY_KEY, "SYP")
    ctx.settings.set(currency.EXCHANGE_RATE_KEY, "13500")

    customer = ctx.customers.create("عميل")
    item = ctx.items.create(name="مادة", selling_price=10, purchase_price=5, quantity=100)
    invoice_id = ctx.invoices.create_invoice(
        invoice_type="sale",
        customer_id=customer,
        paid_amount=4,
        lines=[InvoiceLineInput(description="بند", item_id=item, quantity=1, unit_price=10)],
    )

    # The invoice is already stored with a SYP snapshot; switching the
    # display currency now must not reprice its payment field.
    ctx.settings.set(currency.DISPLAY_CURRENCY_KEY, "USD")

    page = _FakePage()
    center = InvoiceCenter(page, ctx, ft.Container())
    center.show_payment_dialog(invoice_id)

    field = _find_amount_field(page.opened[-1])
    assert field is not None
    assert field.label == "المبلغ (ل.س)"
    assert field.value == "81000"
    assert currency.parse_display_input(field.value, currency.invoice_settings(
        ctx.invoices.get_invoice(invoice_id), ctx.settings
    )) == 6.0
