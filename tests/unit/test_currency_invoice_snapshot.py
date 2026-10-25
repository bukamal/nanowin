"""Invoice currency snapshots for payment/detail screens.

A historical SYP invoice must keep showing SYP amounts even after the
admin switches the app's display currency to USD. The invoice row stores
the rate/symbol in effect at creation; ``currency.invoice_settings`` turns
that snapshot into a settings view the normal formatters can consume.
"""

from __future__ import annotations

from nano_offline.core import currency


def _settings(display: str = "USD") -> dict[str, str]:
    return {
        currency.DISPLAY_CURRENCY_KEY: display,
        currency.EXCHANGE_RATE_KEY: "13500",
        currency.DISPLAY_SYMBOL_KEY: "ل.س",
        currency.USD_DISPLAY_SYMBOL_KEY: "$",
    }


def _invoice(code: str, rate: float, symbol: str) -> dict:
    return {
        "invoice_currency_code": code,
        "invoice_exchange_rate": rate,
        "invoice_currency_symbol": symbol,
    }


def test_syp_invoice_keeps_pounds_after_switching_display_to_usd():
    pay_settings = currency.invoice_settings(_invoice("SYP", 13500, "ل.س"), _settings("USD"))

    assert currency.get_display_currency(pay_settings) == "SYP"
    assert currency.amount_field_label("المبلغ", pay_settings) == "المبلغ (ل.س)"
    assert currency.to_input_text(10, pay_settings) == "135000"
    assert currency.format_amount(10, pay_settings) == "135,000 ل.س"


def test_usd_invoice_stays_in_dollars_after_switching_display_to_syp():
    pay_settings = currency.invoice_settings(_invoice("USD", 13500, "$"), _settings("SYP"))

    assert currency.get_display_currency(pay_settings) == "USD"
    assert currency.amount_field_label("المبلغ", pay_settings) == "المبلغ ($)"
    assert currency.to_input_text(10, pay_settings) == "10"
    assert currency.format_amount(10, pay_settings) == "10.00 $"


def test_invoice_without_snapshot_falls_back_to_current_settings():
    fallback = _settings("SYP")

    assert currency.invoice_settings({"invoice_currency_code": None}, fallback) is fallback
    assert currency.invoice_settings({}, fallback) is fallback
    assert currency.invoice_settings(None, fallback) is fallback
