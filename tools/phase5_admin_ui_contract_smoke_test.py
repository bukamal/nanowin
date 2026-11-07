from pathlib import Path

root = Path(__file__).resolve().parents[1]
main = (root / "src/main.py").read_text(encoding="utf-8")
login = (root / "src/nano_offline/views/login_view.py").read_text(encoding="utf-8")
admin = (root / "src/nano_offline/views/admin_view.py").read_text(encoding="utf-8")
# The Arabic UI strings live in login_view.py (the first-run/create-admin gate),
# while the shell/permission wiring lives in main.py -- check each where it is.
shell = main + "\n" + login
for needle in ["إنشاء المدير الأول", "تسجيل الدخول المحلي", "actions = {", "allowed_keys", "session.can(k)", "AdminCenter"]:
    assert needle in shell, needle
for needle in ["المستخدمون المحليون", "النسخ الاحتياطي والاسترجاع", "سجل التدقيق", "create_backup", "restore_backup"]:
    assert needle in admin, needle
assert "ترخيص كاشير" in admin
assert "audit_rows_view = DataGrid(" in admin
assert "users_grid = DataGrid(" in admin and "backup_grid = DataGrid(" in admin
assert "section_nav," in admin and "_render_section_nav()" in admin
assert "audit_rows_view.on_row_menu" in admin and "users_grid.on_row_menu" in admin
assert "RibbonAction" not in admin and "تحميل المزيد" in admin
print("phase5_admin_ui_contract_smoke_test passed")
