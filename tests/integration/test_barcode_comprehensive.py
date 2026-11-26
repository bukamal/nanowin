"""Comprehensive barcode coverage: every stored type resolves, plus the known
limits of the current model (case sensitivity, label symbology, non-ASCII)."""

from __future__ import annotations

import pytest

from nano_offline.app_context import AppContext
from nano_offline.core import barcode_quality as bq
from nano_offline.core.barcode128 import _encodable, code128b_svg


def _ctx(tmp_path) -> AppContext:
    return AppContext.create(tmp_path / "nano.db")


# Real, checksum-valid codes for each numeric standard.
_EAN13 = "4006381333931"
_EAN8 = "96385074"
_UPCA = "036000291452"


def test_find_by_barcode_resolves_every_stored_type(tmp_path):
    ctx = _ctx(tmp_path)
    unit = ctx.definitions.create_unit("قطعة", "ق")
    codes = {
        "ean13": _EAN13,
        "ean8": _EAN8,
        "upca": _UPCA,
        "code128": "ABC-12345",
        "qr_url": "https://example.com/x?a=1&b=2",
        "leading_zeros": "0000000000017",
        "mixedcase": "AbC123",
    }
    created: dict[str, int] = {}
    for key, code in codes.items():
        created[key] = ctx.items.create(
            name=f"مادة {key}", purchase_price=1, selling_price=2,
            quantity=1, base_unit_id=unit, barcode=code,
        )

    for key, code in codes.items():
        found = ctx.items.find_by_barcode(code)
        assert found is not None, key
        assert int(found["id"]) == created[key], key


def test_find_by_barcode_tolerates_surrounding_whitespace(tmp_path):
    ctx = _ctx(tmp_path)
    unit = ctx.definitions.create_unit("قطعة", "ق")
    item = ctx.items.create(name="أرز", purchase_price=1, selling_price=2, quantity=1, base_unit_id=unit, barcode=_EAN13)
    found = ctx.items.find_by_barcode(f"  {_EAN13}  ")
    assert found is not None and int(found["id"]) == item


def test_find_by_barcode_resolves_secondary_codes_and_labels(tmp_path):
    ctx = _ctx(tmp_path)
    unit = ctx.definitions.create_unit("قطعة", "ق")
    item = ctx.items.create(name="ماء", purchase_price=1, selling_price=2, quantity=1, base_unit_id=unit, barcode="WATER-1")
    ctx.items.add_barcode(item, "CARTON-99", label="كرتون")
    found = ctx.items.find_by_barcode("CARTON-99")
    assert found is not None and int(found["id"]) == item
    assert found.get("matched_label") == "كرتون"


def test_find_by_barcode_is_case_sensitive_known_limitation(tmp_path):
    # Documents the gap: a scanner that upper-cases a lower/mixed-case Code128
    # code will not find it. (Fix = a normalized fallback lookup.)
    ctx = _ctx(tmp_path)
    unit = ctx.definitions.create_unit("قطعة", "ق")
    ctx.items.create(name="مادة", purchase_price=1, selling_price=2, quantity=1, base_unit_id=unit, barcode="abc123")
    assert ctx.items.find_by_barcode("abc123") is not None
    assert ctx.items.find_by_barcode("ABC123") is None


def test_duplicate_barcode_is_rejected_across_both_tables(tmp_path):
    ctx = _ctx(tmp_path)
    unit = ctx.definitions.create_unit("قطعة", "ق")
    ctx.items.create(name="أ", purchase_price=1, selling_price=2, quantity=1, base_unit_id=unit, barcode=_EAN13)
    # Same primary code on another item.
    with pytest.raises(ValueError):
        ctx.items.create(name="ب", purchase_price=1, selling_price=2, quantity=1, base_unit_id=unit, barcode=_EAN13)
    # A secondary code on ANOTHER item that collides with an existing primary.
    other = ctx.items.create(name="ج", purchase_price=1, selling_price=2, quantity=1, base_unit_id=unit, barcode="OTHER-1")
    with pytest.raises(ValueError):
        ctx.items.add_barcode(other, _EAN13)


def test_checksum_detection_and_generation():
    # Valid numeric standards pass; a bad check digit warns; non-numeric is
    # never flagged (Code128/QR have no checksum).
    assert bq.checksum_warning(_EAN13) is None
    assert bq.checksum_warning("4006381333930") is not None
    assert bq.checksum_warning(_EAN8) is None
    assert bq.checksum_warning(_UPCA) is None
    assert bq.checksum_warning("ABC-123") is None

    # Shape detection: only three kinds exist, so EAN-8/UPC-A read as EAN13.
    assert bq.detect_barcode_kind(_EAN13) == "EAN13"
    assert bq.detect_barcode_kind(_EAN8) == "EAN13"   # would ideally be EAN8
    assert bq.detect_barcode_kind(_UPCA) == "EAN13"   # would ideally be UPCA
    assert bq.detect_barcode_kind("ABC-123") == "CODE128"
    assert bq.detect_barcode_kind("https://example.com/a?b=c&d=e") == "QR"

    # Generation: EAN13 always checksum-valid; generated codes are ASCII.
    for _ in range(50):
        ean = bq.generate_barcode_value("EAN13")
        assert len(ean) == 13 and ean.isdigit() and bq.checksum_warning(ean) is None
    assert bq.generate_barcode_value("EAN13", prefix="200").startswith("200")
    for kind, length in (("CODE128", 12), ("QR", 10)):
        code = bq.generate_barcode_value(kind)
        assert len(code) == length and _encodable(code) == code


def test_label_symbology_is_code128_only_and_drops_non_ascii():
    # Known limitation: printed item labels always render Code128B, regardless
    # of the stored "kind"; QR item barcodes are not printed as QR (only the
    # optional price QR is). And non-ASCII payloads lose their characters.
    svg = code128b_svg(_EAN13)
    assert svg.startswith("<svg") and "<rect" in svg
    assert _encodable(_EAN13) == _EAN13
    assert _encodable("طعام ١٢٣") == " "  # Arabic letters/digits dropped, space kept
