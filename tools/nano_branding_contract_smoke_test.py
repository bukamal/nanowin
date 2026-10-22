from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
main = (ROOT / "src/main.py").read_text(encoding="utf-8")
pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
activation = (ROOT / "src/nano_offline/views/activation_view.py").read_text(encoding="utf-8")
license_service = (ROOT / "src/nano_offline/services/license_service.py").read_text(encoding="utf-8")

assert 'name = "nano-offline"' in pyproject
assert 'product = "Nano"' in pyproject
assert 'org = "com.nano"' in pyproject
assert 'page.title = "Nano | نانو"' in main
assert 'src="icon.png"' in main
assert 'src="icon.png"' in activation
assert 'f"Nano-Offline/{version_token}"' in license_service

assets = ROOT / "src/assets"
for name in [
    "icon.png", "icon_android.png", "icon_ios.png", "icon_web.png",
    "icon_macos.png", "icon_windows.ico", "splash.png", "splash_dark.png",
]:
    assert (assets / name).is_file(), name

# Navigation contract: the desktop ribbon (CommandBar) is the navigation
# surface; the mobile bottom bar / FAB / "المزيد" sheet are gone.
assert 'CommandBar' in main
assert 'RibbonTab' in main
assert 'command_bar' in main

print("nano_branding_contract_smoke_test passed")
