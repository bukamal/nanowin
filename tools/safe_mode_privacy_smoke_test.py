from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
privacy_src = (ROOT / "src/nano_offline/core/privacy.py").read_text(encoding="utf-8")
main = (ROOT / "src/main.py").read_text(encoding="utf-8")
dash = (ROOT / "src/nano_offline/views/dashboard_view.py").read_text(encoding="utf-8")
items = (ROOT / "src/nano_offline/views/items_view.py").read_text(encoding="utf-8")

# Core module surface.
for token in [
    'SAFE_MODE_KEY = "ui_safe_mode"',
    "MASK_TEXT",
    "PRIVILEGED_ROLES",
    "FRONT_ROLES",
    "def is_safe_mode",
    "def set_safe_mode",
    "def effective_safe_mode",
    "def money",
    "def money_ctx",
]:
    assert token in privacy_src, token

# Behaviour: safe mode masks money; front-line roles are always hidden.
import importlib.util
import sys

sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("privacy", ROOT / "src/nano_offline/core/privacy.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class _Settings:
    def __init__(self, data):
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


settings = _Settings({})
assert privacy.is_safe_mode(settings) is False
assert privacy.money(1000, settings, "admin").strip() != privacy.MASK_TEXT  # off for privileged -> number
assert privacy.effective_safe_mode(settings, "sales") is True  # front-line forced
assert privacy.is_forced("sales") is True and privacy.can_reveal("admin") is True
privacy.set_safe_mode(settings, True)
assert privacy.money(1000, settings, "admin").strip() == privacy.MASK_TEXT
assert privacy.is_safe_mode(settings) is True

# The status bar drops the cash amount and hosts the safe-mode toggle.
assert "cash_text" not in main
assert "safe_btn" in main
assert "_toggle_safe" in main
assert "الوضع الآمن" in main

# Private figures route through privacy; selling prices do not.
assert "privacy.money_ctx" in dash
assert "private_money" in items

print("safe_mode_privacy_smoke_test passed")
