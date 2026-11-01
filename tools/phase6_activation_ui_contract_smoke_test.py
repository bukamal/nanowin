from pathlib import Path

root = Path(__file__).resolve().parents[1]
main = (root / "src/main.py").read_text(encoding="utf-8")
view = (root / "src/nano_offline/views/activation_view.py").read_text(encoding="utf-8")
admin = (root / "src/nano_offline/views/admin_view.py").read_text(encoding="utf-8")
license_code = (root / "src/nano_offline/services/license_service.py").read_text(encoding="utf-8")

for needle in ["ActivationGate", "ctx.license.status().valid", "show_activation", "show_auth"]:
    assert needle in main, needle
for needle in ["تفعيل {branding.APP_BRAND}", "licenseCode", "fingerprint"]:
    assert (needle in view) or (needle in license_code), needle
assert "ترخيص كاشير" in admin
assert "معرّف الجهاز (بيانات خاصة)" in admin
assert "activation_url()" not in admin
assert "status.protocol" not in admin
assert "RSA public modulus" not in admin
assert "عنوان خادم التفعيل HTTPS" not in admin
print("phase6_activation_ui_contract_smoke_test passed")
