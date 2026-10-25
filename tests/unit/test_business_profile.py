from __future__ import annotations

from nano_offline.core import business_profile


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_business_profile_defaults_to_retail():
    profile = business_profile.profile(_Settings())

    assert profile["business_type"] == "retail"
    assert profile["sale_title"] == "فاتورة بيع"
    assert profile["item_label"] == "المادة"
    assert profile["customer_label"] == "العميل"
    assert profile["phone"] == ""


def test_business_profile_uses_restaurant_labels_and_contact_fields():
    profile = business_profile.profile(
        _Settings(
            business_type="restaurant",
            business_phone=" 0999 111 222 ",
            business_address="شارع النيل",
            business_tax_number="TAX-9",
        )
    )

    assert profile["sale_title"] == "فاتورة طلب"
    assert profile["item_label"] == "الصنف"
    assert profile["quantity_label"] == "العدد"
    assert profile["customer_label"] == "الزبون"
    assert profile["reference_label"] == "رقم الطلب / الطاولة"
    assert profile["phone"] == "0999 111 222"
    assert profile["address"] == "شارع النيل"
    assert profile["tax_number"] == "TAX-9"


def test_unknown_business_type_falls_back_to_retail():
    assert business_profile.normalize_type("unknown") == "retail"
    assert business_profile.labels_for_type(None)["sale_title"] == "فاتورة بيع"
