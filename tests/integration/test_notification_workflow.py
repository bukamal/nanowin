from __future__ import annotations

from datetime import date, timedelta

from nano_offline.app_context import AppContext
from nano_offline.services.invoice_service import InvoiceLineInput


def test_business_alerts_are_created_deduplicated_and_marked_read(tmp_path):
    ctx = AppContext.create(tmp_path / "notifications.db")
    ctx.auth.create_initial_admin("admin", "المدير", "StrongPass1")
    ctx.auth.login("admin", "StrongPass1")
    ctx.notifications.set_external_hook(None)
    ctx.notifications.save_config(
        {
            "receivables": {"enabled": True, "overdue_after_days": 30, "remind_before_days": 3},
            "low_stock": {"enabled": True, "default_threshold": 5},
            "backup": {"enabled": False},
            "license": {"enabled": False},
            "insights": {"enabled": False},
            "quiet_hours": {"enabled": False},
        }
    )

    unit = ctx.definitions.create_unit("قطعة", "ق")
    customer = ctx.customers.create("عميل متأخر")
    supplier = ctx.suppliers.create("مورد")
    item = ctx.items.create(
        name="مادة منخفضة",
        item_type="مخزون",
        purchase_price=10.0,
        selling_price=15.0,
        quantity=0.0,
        base_unit_id=unit,
        barcode="NOTIFY-1",
    )
    ctx.invoices.create_invoice(
        invoice_type="purchase",
        supplier_id=supplier,
        invoice_date=(date.today() - timedelta(days=41)).isoformat(),
        lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=3, unit_price=10.0)],
    )
    ctx.invoices.create_invoice(
        invoice_type="sale",
        customer_id=customer,
        invoice_date=(date.today() - timedelta(days=40)).isoformat(),
        lines=[InvoiceLineInput(description="مادة", item_id=item, quantity=1, unit_price=15.0)],
    )

    alerts = ctx.notifications.generate_alerts()
    assert {alert.rule_key for alert in alerts} == {"low_stock", "receivables_overdue"}

    ctx.notifications.sync()
    assert ctx.notifications.unread_count() == 2
    assert ctx.notifications.unread_summary() == {"urgent": 1, "warning": 1, "info": 0, "total": 2}

    ctx.notifications.sync()
    assert ctx.notifications.unread_count() == 2

    overdue = next(row for row in ctx.notifications.recent() if row["rule_key"] == "receivables_overdue")
    ctx.notifications.mark_read(int(overdue["id"]))
    assert ctx.notifications.unread_summary() == {"urgent": 0, "warning": 1, "info": 0, "total": 1}


def test_receivable_alerts_follow_invoice_due_dates_and_repeat_window(tmp_path):
    ctx = AppContext.create(tmp_path / "notification-due-dates.db")
    ctx.settings.set("invoice_default_due_days", "10")
    ctx.notifications.save_config(
        {
            "receivables": {"enabled": True, "overdue_after_days": 30, "remind_before_days": 3},
            "low_stock": {"enabled": False},
            "backup": {"enabled": False},
            "license": {"enabled": False},
            "insights": {"enabled": False},
            "quiet_hours": {"enabled": False},
            "repeat_after_days": 3,
        }
    )
    unit = ctx.definitions.create_unit("قطعة", "ق")
    item = ctx.items.create(
        name="مادة آجلة",
        item_type="مخزون",
        purchase_price=5,
        selling_price=10,
        quantity=100,
        base_unit_id=unit,
    )
    customer = ctx.customers.create("عميل آجل")

    def sale(invoice_date: date, due_days: int):
        ctx.settings.set("invoice_default_due_days", str(due_days))
        return ctx.invoices.create_invoice(
            invoice_type="sale",
            customer_id=customer,
            invoice_date=invoice_date.isoformat(),
            lines=[InvoiceLineInput(description="مادة آجلة", item_id=item, unit_id=unit, quantity=1, unit_price=10)],
        )

    overdue_id = sale(date.today() - timedelta(days=12), 10)
    due_soon_id = sale(date.today() - timedelta(days=8), 10)
    not_overdue_id = sale(date.today() - timedelta(days=40), 60)

    alerts = ctx.notifications.generate_alerts()
    overdue = next(alert for alert in alerts if alert.rule_key == "receivables_overdue")
    due_soon = next(alert for alert in alerts if alert.rule_key == "receivables_due_soon")
    assert overdue.entity_type == "invoice" and overdue.entity_id == overdue_id
    assert due_soon.entity_type == "invoice" and due_soon.entity_id == due_soon_id
    assert str(not_overdue_id) not in overdue.body
    assert ctx.notifications._dedupe_key("test", date(2026, 1, 1), {"repeat_after_days": 3}) == ctx.notifications._dedupe_key(
        "test",
        date.fromisoformat(ctx.notifications._dedupe_key("test", date(2026, 1, 1), {"repeat_after_days": 3}).split(":")[1]) + timedelta(days=1),
        {"repeat_after_days": 3},
    )
    assert ctx.notifications._dedupe_key("test", date(2026, 1, 1), {"repeat_after_days": 3}) != ctx.notifications._dedupe_key(
        "test", date(2026, 1, 4), {"repeat_after_days": 3}
    )
