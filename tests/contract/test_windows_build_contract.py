from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_windows_build_fails_if_required_app_bundle_is_missing():
    workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")

    assert 'throw "app.zip not found in any expected Flet build location' in workflow
    assert 'throw "bundled app.zip has no main.py/main.pyc at its root"' in workflow
    assert 'throw "bundled app.zip has no nano_offline package at its root"' in workflow
    assert 'flet build windows --product "Nano"' in workflow
    assert "skipping entry-point guard" not in workflow
