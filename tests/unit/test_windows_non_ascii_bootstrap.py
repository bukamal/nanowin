import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_vendored_serious_python_windows_uses_wide_char_paths():
    """The Windows bootstrap must not pass UTF-8 paths to the ANSI CRT.

    Flet 0.28.3 pins serious_python 0.9.11, whose ``_putenv_s``/``fopen_s``
    calls mangle any path with Arabic (or other non-ANSI) characters. The
    vendored copy carries the upstream 4.3.6 wide-char fix plus direct
    sys.path/os.environ setup.
    """
    cpp = (
        ROOT
        / "vendor"
        / "serious_python_windows"
        / "windows"
        / "serious_python_windows_plugin.cpp"
    ).read_text(encoding="utf-8")

    assert "_wputenv_s" in cpp
    assert "_wfopen_s" in cpp
    assert "PYTHONUTF8" in cpp
    assert "InsertSysPath" in cpp
    assert "SetPythonEnv" in cpp
    # No leftover narrow CRT calls (the wide names contain the narrow
    # substring, so strip them before checking).
    narrow = cpp.replace("_wputenv_s(", "").replace("_wfopen_s(", "")
    assert "_putenv_s(" not in narrow
    assert "fopen_s(" not in narrow


def test_windows_build_overrides_the_vendored_plugin():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    override = pyproject["tool"]["flet"]["flutter"]["pubspec"][
        "dependency_overrides"
    ]["serious_python_windows"]
    assert override["path"] == "../../vendor/serious_python_windows"
