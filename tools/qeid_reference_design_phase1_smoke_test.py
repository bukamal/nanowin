from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
main = (ROOT / "src/main.py").read_text(encoding="utf-8")
invoice = (ROOT / "src/nano_offline/views/invoice_view.py").read_text(encoding="utf-8")
dashboard = (ROOT / "src/nano_offline/views/dashboard_view.py").read_text(encoding="utf-8")

for token in [
    # Desktop-only shell: ribbon navigation replaces the mobile bottom bar.
    "CommandBar",
    "RibbonTab",
    "_section_actions",
    "command_bar",
    "branding.APP_TAGLINE",
]:
    assert token in main, token

# Dashboard body content moved into DashboardCenter (nano_offline/views/dashboard_view.py)
# as part of the 2026-08 refactor that split main.py's view closures into
# Center classes; same functional guarantee, new location.
for token in [
    # Dashboard redesign (2026-08): quick actions became a thin icon strip
    # (no "إجراءات سريعة" header) and the smart surfaces merged into one
    # "ذكاء الأعمال" card. Pin the section titles that remain.
    '"ذكاء الأعمال"',
    '"المبيعات والمشتريات"',
    '"آخر الفواتير"',
    '"استمر بأداء عملك — لا توجد تنبيهات مهمة حاليًا."',
]:
    assert token in dashboard, token

for token in [
    'label="بحث في الفواتير"',
    'filters = {"type": initial_type, "status": "all"}',
    'chip("مبيعات"',
    'chip("مشتريات"',
    'action_label="بيع جديد"',
    "def show_editor",
]:
    assert token in invoice, token

assert "NavigationRail(" not in main
assert "NavigationBar(" not in main
print("qeid_reference_design_phase1_smoke_test passed")
