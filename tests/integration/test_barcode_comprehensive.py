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


def test_find_by_barcode_falls_back_case_insensitively(tmp_path):
    # A scanner may upper/lower-case a Code128 token; the fallback resolves it.
    ctx = _ctx(tmp_path)
    unit = ctx.definitions.create_unit("قطعة", "ق")
    ctx.items.create(name="مادة", purchase_price=1, selling_price=2, quantity=1, base_unit_id=unit, barcode="abc123")
    assert ctx.items.find_by_barcode("abc123") is not None
    assert ctx.items.find_by_barcode("ABC123") is not None


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


def test_label_symbology_matches_the_code_shape():
    from nano_offline.core.ean13 import ean13_modules, ean13_svg
    from nano_offline.services.document_service import _label_barcode_svg

    # Valid EAN-13 / UPC-A -> true EAN modules (95), rendered as an SVG.
    assert ean13_modules(_EAN13) is not None and len(ean13_modules(_EAN13)) == 95
    assert ean13_modules(_UPCA) is not None  # UPC-A padded to EAN-13
    assert ean13_modules("4006381333930") is None  # bad check digit
    assert ean13_svg(_EAN13).startswith("<svg")
    assert ean13_svg("4006381333930") == ""

    ean = _label_barcode_svg(_EAN13, width=210, height=54, show_text=True)
    assert ean.startswith("<svg") and 'viewBox="0 0 210 54"' in ean
    # A long/punctuation payload becomes a square QR (min side = 64 here).
    qr = _label_barcode_svg("https://example.com/a?b=c&d=e", width=210, height=54, show_text=True)
    assert qr.startswith("<svg") and 'viewBox="0 0 64 64"' in qr
    # A plain alnum SKU stays Code128 at the requested size.
    code128 = _label_barcode_svg("ABC-12345", width=210, height=54, show_text=True)
    assert 'viewBox="0 0 210 54"' in code128


def test_code128_label_still_drops_non_ascii():
    # Remaining limit (Code128 subset B is ASCII-only): a *short* non-ASCII code
    # that isn't QR-shaped loses its characters. Numeric/alnum codes are fine.
    svg = code128b_svg(_EAN13)
    assert svg.startswith("<svg") and "<rect" in svg
    assert _encodable(_EAN13) == _EAN13
    assert _encodable("طعام ١٢٣") == " "  # Arabic letters/digits dropped, space kept
