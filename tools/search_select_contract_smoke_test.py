from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Contract: mostly customer-facing screens must use search-select, not raw
# Dropdown. Screens and reusable controls that legitimately use ft.Dropdown
# for a focused interaction are pinned as EXEMPT so the intent stays explicit.
EXEMPT = ['admin_view.py', 'finance_view.py', 'items_view.py', 'notifications_view.py', 'reports_view.py']
INVOICE_VIEW = ROOT / "src" / "nano_offline" / "views" / "invoice_view.py"
DATA_GRID = ROOT / "src" / "nano_offline" / "components" / "data_grid.py"
invoice_text = INVOICE_VIEW.read_text(encoding="utf-8")
# invoice_view owns exactly one sanctioned Dropdown: the per-invoice-line unit
# picker (a small fixed option list, deliberately switched from a two-tap
# search field to a one-tap dropdown by design). Pin that exception, forbid
# any other Dropdown anywhere in the screen.
assert invoice_text.count("ft.Dropdown(") == 1, invoice_text.count("ft.Dropdown(")
dropdown_start = invoice_text.find("ft.Dropdown(")
assert "الوحدة" in invoice_text[dropdown_start : dropdown_start + 120], "sanctioned dropdown must be the unit picker"
# DataGrid no longer uses a raw Dropdown: picker cells render an inline
# search panel inside the grid body; other components keep using SearchSelect.
grid_text = DATA_GRID.read_text(encoding="utf-8")
assert grid_text.count("ft.Dropdown(") == 0, grid_text.count("ft.Dropdown(")
assert "_picker_panel" in grid_text, "picker must render an inline panel"

joined = ""
for p in (ROOT / "src").rglob("*.py"):
    if p.name in EXEMPT:
        continue
    text = p.read_text(encoding="utf-8")
    if p == INVOICE_VIEW:
        text = text.replace("ft.Dropdown(", "ft.SearchSelect(")
    elif p == DATA_GRID:
        text = text.replace("ft.Dropdown(", "ft.SearchSelect(")
    joined += text
assert "ft.Dropdown(" not in joined, "ft.Dropdown( outside exempt screens"
print("search_select_contract_smoke_test passed")
