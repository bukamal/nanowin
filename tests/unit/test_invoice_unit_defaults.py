from nano_offline.views.invoice_view import _default_invoice_unit


def test_piece_unit_is_preferred_over_item_base_unit():
    base = {"id": 1, "name": "كرتون", "is_base": True, "conversion_factor": 12}
    piece = {"id": 2, "name": "قطعة", "is_base": False, "conversion_factor": 1}

    assert _default_invoice_unit([base, piece]) is piece


def test_item_base_unit_is_fallback_when_piece_is_not_allowed():
    base = {"id": 1, "name": "كيلوغرام", "is_base": True, "conversion_factor": 1}
    alternate = {"id": 2, "name": "طن", "is_base": False, "conversion_factor": 1000}

    assert _default_invoice_unit([base, alternate]) is base


def test_missing_base_unit_falls_back_to_first_available_unit():
    alternate = {"id": 2, "name": "علبة", "is_base": False, "conversion_factor": 6}

    assert _default_invoice_unit([alternate]) is alternate
    assert _default_invoice_unit([]) is None
