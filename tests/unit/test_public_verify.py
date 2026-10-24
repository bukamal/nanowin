import sqlite3

from nano_offline.core.public_verify import public_fingerprint, verify_manual


class MemoryDatabase:
    def __init__(self, prefix: str):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE invoices(
                id INTEGER PRIMARY KEY, invoice_date TEXT, total REAL,
                customer_id INTEGER, supplier_id INTEGER
            );
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE invoice_lines(id INTEGER PRIMARY KEY, invoice_id INTEGER);
            """
        )
        self.connection.execute(
            "INSERT INTO settings(key, value) VALUES('invoice_number_prefix', ?)",
            (prefix,),
        )
        self.connection.execute(
            "INSERT INTO invoices(id, invoice_date, total, customer_id, supplier_id) "
            "VALUES(12, '2026-10-24', 25.5, NULL, NULL)"
        )
        self.connection.commit()

    def connect(self):
        return self.connection


def test_verify_manual_parses_numeric_display_prefix_without_mixing_digits():
    db = MemoryDatabase("2026-INV-")
    token = public_fingerprint(
        invoice_id=12,
        invoice_date="2026-10-24",
        total=25.5,
        party_key="-",
        lines=0,
    )

    valid, _, invoice = verify_manual(db, "2026-INV-00012", token)

    assert valid
    assert invoice["id"] == 12


def test_verify_manual_accepts_internal_invoice_id_with_hash():
    db = MemoryDatabase("2026-INV-")
    token = public_fingerprint(
        invoice_id=12,
        invoice_date="2026-10-24",
        total=25.5,
        party_key="-",
        lines=0,
    )

    valid, _, invoice = verify_manual(db, "#12", token)

    assert valid
    assert invoice["id"] == 12
