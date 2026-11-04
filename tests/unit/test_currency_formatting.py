"""Thousands separators / decimals for stored-USD amounts shown as USD or SYP.

Amounts are always stored in USD. On screen they are either shown as the
*stored* USD value (``format_amount``) or as an already-converted display
value (``format_display_value``). Both must use thousands separators and the
display currency's own decimals: 2 for USD, 0 for SYP.
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


def test_usd_amount_has_separators_two_decimals_and_symbol():
    s = _settings("USD")

    assert currency.format_amount(126.0, s) == "126.00 $"
    assert currency.format_amount(1234.5, s) == "1,234.50 $"
    assert currency.format_amount(12000, s) == "12,000.00 $"


def test_syp_amount_has_separators_zero_decimals_and_symbol():
    s = _settings("SYP")

    # 1 USD * 13500 -> 13,500 ل.س (no fractional pounds)
    assert currency.format_amount(1.0, s) == "13,500 ل.س"
    assert currency.format_amount(78.75, s) == "1,063,125 ل.س"
    assert "." not in currency.format_amount(78.75, s, with_symbol=False)


def test_display_value_adds_separators_and_respects_display_decimals():
    usd = _settings("USD")
    syp = _settings("SYP")

    # Already-converted value, no symbol (the invoice-table columns).
    assert currency.format_display_value(1234.5, usd, with_symbol=False) == "1,234.50"
    assert currency.format_display_value(1701000, syp, with_symbol=False) == "1,701,000"
    assert currency.format_display_value(1701000, syp) == "1,701,000 ل.س"
    assert currency.format_display_value(1234.5, usd) == "1,234.50 $"


def test_parse_display_input_round_trips_with_separators():
    usd = _settings("USD")
    syp = _settings("SYP")

    assert currency.parse_display_input("1,234.50", usd) == 1234.5
    # 13,500 ل.س -> 1.00 USD at the configured 13500 rate.
    assert currency.parse_display_input("13,500", syp) == 1.0
