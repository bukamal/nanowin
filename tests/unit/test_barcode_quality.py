"""Pure barcode-quality helpers: checksum validation, kind detection,
similar-code detection, and generation."""

from __future__ import annotations

from nano_offline.core import barcode_quality as bq

VALID_EAN13 = "4006381333931"
VALID_EAN8 = "96385074"
VALID_UPCA = "036000291452"


def _flip_last(code: str) -> str:
    last = str((int(code[-1]) + 1) % 10)
    return code[:-1] + last


def test_checksum_warning_accepts_valid_numeric_barcodes():
    for code in (VALID_EAN13, VALID_EAN8, VALID_UPCA):
        assert bq.checksum_warning(code) is None


def test_checksum_warning_flags_bad_check_digit():
    assert bq.checksum_warning(_flip_last(VALID_EAN13)) is not None


def test_checksum_warning_ignores_non_numeric_and_unknown_lengths():
    assert bq.checksum_warning("SKU-ABC") is None
    assert bq.checksum_warning("12345") is None  # not an EAN/UPC length
    assert bq.checksum_warning("") is None


def test_generate_ean13_is_valid_and_honours_prefix():
    code = bq.generate_barcode_value("EAN13")
    assert len(code) == 13 and code.isdigit()
    assert bq.checksum_warning(code) is None

    prefixed = bq.generate_barcode_value("EAN13", prefix="200")
    assert prefixed.startswith("200") and len(prefixed) == 13
    assert bq.checksum_warning(prefixed) is None


def test_generate_other_kinds_have_expected_shape():
    c128 = bq.generate_barcode_value("CODE128")
    assert len(c128) == 12 and c128.isalnum()
    qr = bq.generate_barcode_value("QR")
    assert len(qr) == 10 and qr.isalnum()
    # Unknown kind falls back to the QR shape.
    assert len(bq.generate_barcode_value("WHAT")) == 10


def test_detect_barcode_kind():
    assert bq.detect_barcode_kind("") is None
    assert bq.detect_barcode_kind(VALID_EAN13) == "EAN13"
    assert bq.detect_barcode_kind("ABC-123") == "CODE128"
    assert bq.detect_barcode_kind("https://example.com/p/1234567890") == "QR"


def test_find_similar_by_one_edit_and_excludes_exact_match():
    candidates = [
        ("4006381333931", "الأصلي"),
        ("4006381333932", "قريب جدًا"),   # one substitution away
        ("9999999999999", "بعيد"),
    ]
    names = bq.find_similar("4006381333931", candidates)
    assert "الأصلي" not in names          # exact match is a duplicate, not similar
    assert "قريب جدًا" in names
    assert "بعيد" not in names
    assert bq.find_similar("", candidates) == []
