from __future__ import annotations

import sqlite3

import pytest

from nano_offline.core import business_profile
from nano_offline.core import invoice_settings
from nano_offline.core.database import Database
from nano_offline.services.document_service import DocumentService
from nano_offline.services.invoice_service import InvoiceLineInput, InvoiceService
from nano_offline.services.statement_service import StatementService


def _render_invoice(tmp_path, extra_settings: dict[str, str] | None = None) -> str:
    db = Database(tmp_path / "invoice-print.db")
    db.initialize()
    if extra_settings:
        with db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                list(extra_settings.items()),
            )
    invoices = InvoiceService(db)
    invoice_id = invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="مادة تجريبية", quantity=2, unit_price=10)],
        paid_amount=5,
    )
    html = DocumentService(db, invoices, StatementService(db)).invoice_html(invoice_id)
    return html.split("<body>", 1)[1]


def test_invoice_print_defaults_to_clean_layout(tmp_path):
    body = _render_invoice(tmp_path)

    assert "رقم الفاتورة:" not in body
    assert '<span class="num-barcode">' not in body
    assert '<div class="rate-note">' not in body
    assert '<div class="progress-wrap">' not in body
    assert '<div class="verify-box">' not in body
    assert "<th>المادة</th><th>الكمية</th><th>السعر</th><th>الإجمالي</th>" in body
    assert 'class="totals-table"' in body
    assert "الإجمالي" in body
    assert "المتبقي" not in body


def test_invoice_print_optional_blocks_follow_admin_settings(tmp_path):
    body = _render_invoice(
        tmp_path,
        {
            invoice_settings.SHOW_NUMBER_KEY: "1",
            invoice_settings.SHOW_BARCODE_KEY: "1",
            invoice_settings.SHOW_EXCHANGE_RATE_KEY: "1",
            invoice_settings.SHOW_PROGRESS_KEY: "1",
            invoice_settings.SHOW_REMAINING_KEY: "1",
            invoice_settings.SHOW_VERIFY_QR_KEY: "1",
        },
    )

    assert "رقم الفاتورة:" in body
    assert '<span class="num-barcode">' in body
    assert '<div class="rate-note">' in body
    assert '<div class="progress-wrap">' in body
    assert "المتبقي" in body
    assert '<div class="verify-box">' in body


def _create_invoice(
    tmp_path,
    extra_settings: dict[str, str] | None = None,
    *,
    with_customer: bool = False,
):
    db = Database(tmp_path / "invoice-snapshot.db")
    db.initialize()
    if extra_settings:
        with db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                list(extra_settings.items()),
            )
    customer_id = None
    if with_customer:
        with db.connect() as conn:
            cur = conn.execute("INSERT INTO customers(name) VALUES('عميل آجل')")
            customer_id = int(cur.lastrowid)
    invoices = InvoiceService(db)
    invoice_id = invoices.create_invoice(
        invoice_type="sale",
        customer_id=customer_id,
        lines=[InvoiceLineInput(description="مادة تجريبية", quantity=2, unit_price=10)],
        paid_amount=5,
    )
    return db, invoices, invoice_id


def _render_body(db, invoices, invoice_id: int) -> str:
    html = DocumentService(db, invoices, StatementService(db)).invoice_html(invoice_id)
    return html.split("<body>", 1)[1]


def test_invoice_print_snapshot_survives_later_admin_changes(tmp_path):
    db, invoices, invoice_id = _create_invoice(
        tmp_path,
        {
            invoice_settings.SHOW_NUMBER_KEY: "0",
            invoice_settings.SHOW_EXCHANGE_RATE_KEY: "1",
            invoice_settings.FOOTER_TEXT_KEY: "الشروط الأصلية",
            invoice_settings.DEFAULT_DUE_DAYS_KEY: "15",
            business_profile.BUSINESS_TYPE_KEY: "restaurant",
        },
        with_customer=True,
    )
    first = _render_body(db, invoices, invoice_id)
    assert "فاتورة طلب" in first
    assert "<th>الصنف</th><th>العدد</th>" in first
    assert "الشروط الأصلية" in first
    assert "الاستحقاق:" in first
    assert "رقم الفاتورة:" not in first

    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            [
                (invoice_settings.SHOW_NUMBER_KEY, "1"),
                (invoice_settings.SHOW_BARCODE_KEY, "1"),
                (invoice_settings.SHOW_EXCHANGE_RATE_KEY, "0"),
                (invoice_settings.SHOW_PROGRESS_KEY, "1"),
                (invoice_settings.SHOW_VERIFY_QR_KEY, "1"),
                (invoice_settings.FOOTER_TEXT_KEY, "الشروط الجديدة"),
                (invoice_settings.DEFAULT_DUE_DAYS_KEY, "0"),
                (business_profile.BUSINESS_TYPE_KEY, "services"),
            ],
        )

    second = _render_body(db, invoices, invoice_id)
    assert second == first

    new_id = invoices.create_invoice(
        invoice_type="sale",
        lines=[InvoiceLineInput(description="خدمة تجريبية", quantity=1, unit_price=10)],
        paid_amount=10,
    )
    new_body = _render_body(db, invoices, new_id)
    assert "فاتورة خدمات" in new_body
    assert "<th>الخدمة</th><th>المدة</th>" in new_body
    assert "رقم الفاتورة:" in new_body
    assert "الشروط الجديدة" in new_body
    assert "الشروط الأصلية" not in new_body


def test_legacy_invoice_without_snapshot_uses_live_settings(tmp_path):
    db, invoices, invoice_id = _create_invoice(tmp_path)
    with db.connect() as conn:
        conn.execute("UPDATE invoices SET print_options=NULL WHERE id=?", (invoice_id,))
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (invoice_settings.SHOW_NUMBER_KEY, "1"),
        )

    body = _render_body(db, invoices, invoice_id)

    assert "رقم الفاتورة:" in body


def test_print_snapshot_merges_missing_keys_from_live_settings():
    snapshot = invoice_settings.print_snapshot(
        {"print_options": '{"show_number": false}'},
        {
            invoice_settings.SHOW_NUMBER_KEY: "1",
            invoice_settings.FOOTER_TEXT_KEY: "شروط حالية",
            "business_type": "restaurant",
        },
    )

    assert snapshot["show_number"] is False
    assert snapshot["footer_text"] == "شروط حالية"
    assert snapshot["business_type"] == "restaurant"


def test_invoice_header_uses_business_profile_contact_details(tmp_path):
    body = _render_invoice(
        tmp_path,
        {
            business_profile.BUSINESS_PHONE_KEY: "0999 111 222",
            business_profile.BUSINESS_ADDRESS_KEY: "شارع النيل",
            business_profile.BUSINESS_TAX_NUMBER_KEY: "TAX-1",
        },
    )

    assert "هاتف:" in body
    assert "0999 111 222" in body
    assert "شارع النيل" in body
    assert "الرقم الضريبي:" in body


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35),
    reason="DROP COLUMN requires SQLite 3.35+",
)
def test_schema_migration_adds_and_backfills_print_options(tmp_path):
    db, _invoices, invoice_id = _create_invoice(tmp_path)
    with db.connect() as conn:
        conn.execute("ALTER TABLE invoices DROP COLUMN print_options")
        conn.execute("UPDATE schema_meta SET value='16' WHERE key='schema_version'")

    db.initialize()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT print_options FROM invoices WHERE id=?", (invoice_id,)
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert row["print_options"]
    assert version == "17"
