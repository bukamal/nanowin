"""Route a sale invoice's lines to their sections and print station tickets.

Restaurant model: when a sale is saved, each line's section (item override,
then category default) gets a price-less preparation ticket printed on that
section's network ESC/POS printer. Printing is durable: jobs are written to
``print_jobs`` first and dispatched on a background thread, so a slow or
offline printer never blocks or fails the sale and a failed job can be
retried later.
"""

from __future__ import annotations

import logging
import threading

from nano_offline.core import escpos
from nano_offline.core.database import Database
from nano_offline.repositories.section_repository import SectionRepository

_LOG = logging.getLogger(__name__)

#: Give up (mark 'failed') after this many dispatch attempts.
MAX_ATTEMPTS = 3


class SectionPrintService:
    def __init__(self, db: Database, sections: SectionRepository):
        self.db = db
        self.sections = sections
        # Tests disable this so enqueueing never opens a socket in the
        # background; production leaves it on.
        self.auto_dispatch = True
        self._dispatch_lock = threading.Lock()

    # ---- enqueue ------------------------------------------------------- #

    def enqueue_invoice(self, invoice_id: int, *, dispatch: bool | None = None) -> int:
        """Create pending print jobs for a sale invoice's sections.

        Returns the number of *new* jobs. A no-op when the invoice is not a
        sale or no line resolves to an active, configured section.
        """
        invoice = self._invoice_header(invoice_id)
        if invoice is None or str(invoice.get("type")) != "sale":
            return 0
        grouped = self.sections.invoice_section_lines(int(invoice_id))
        if not grouped:
            return 0
        enqueued = 0
        for section_id, lines in grouped.items():
            section = self.sections.get(int(section_id))
            if section is None or not int(section.get("active") or 0):
                continue
            host = str(section.get("printer_host") or "").strip()
            if not host:
                continue
            try:
                payload = escpos.build_station_ticket(section, invoice, lines)
            except Exception:
                _LOG.exception("failed to build station ticket for section %s", section_id)
                continue
            if self.sections.enqueue_job(
                invoice_id=int(invoice_id),
                section_id=int(section_id),
                printer_host=host,
                printer_port=int(section.get("printer_port") or 9100),
                payload=payload,
                dedupe_key=f"invoice:{int(invoice_id)}:section:{int(section_id)}",
            ):
                enqueued += 1
        should_dispatch = self.auto_dispatch if dispatch is None else dispatch
        if enqueued and should_dispatch:
            self.dispatch_async()
        return enqueued

    # ---- dispatch ------------------------------------------------------ #

    def dispatch_pending(self, limit: int = 50, *, sender=None) -> dict:
        """Send pending jobs in order. ``sender`` is injectable for tests."""
        send = sender or escpos.send_bytes
        result = {"sent": 0, "failed": 0, "pending": 0}
        for job in self.sections.pending_jobs(limit=limit):
            try:
                send(
                    str(job["printer_host"]),
                    int(job["printer_port"] or 9100),
                    bytes(job["payload"]),
                )
            except Exception as exc:  # noqa: BLE001 - any network error is a retry
                attempts = int(job.get("attempts") or 0) + 1
                final = attempts >= MAX_ATTEMPTS
                self.sections.mark_failed(int(job["id"]), str(exc), final=final)
                result["failed" if final else "pending"] += 1
            else:
                self.sections.mark_sent(int(job["id"]))
                result["sent"] += 1
        return result

    def dispatch_async(self) -> None:
        """Drain the queue on a daemon thread (one drain at a time)."""
        if not self._dispatch_lock.acquire(blocking=False):
            return

        def _run() -> None:
            try:
                self.dispatch_pending()
            except Exception:
                _LOG.exception("station print dispatch failed")
            finally:
                self._dispatch_lock.release()

        threading.Thread(target=_run, name="nano-station-print", daemon=True).start()

    def test_print(self, section_id: int) -> None:
        section = self.sections.get(int(section_id))
        if section is None:
            raise ValueError("القسم غير موجود")
        host = str(section.get("printer_host") or "").strip()
        if not host:
            raise ValueError("لم يتم تحديد عنوان طابعة القسم")
        escpos.send_bytes(
            host,
            int(section.get("printer_port") or 9100),
            escpos.test_ticket(section),
        )

    def retry_failed(self) -> int:
        return self.sections.retry_failed()

    def reprint_invoice(self, invoice_id: int) -> int:
        """Re-send an invoice's station tickets (manual "send to kitchen").

        Clears any prior jobs for the invoice first, so a manual resend after
        adding/editing never duplicates and a previously failed job is retried.
        """
        self.sections.delete_jobs_for_invoice(int(invoice_id))
        return self.enqueue_invoice(int(invoice_id))

    def counts(self) -> dict:
        return self.sections.counts()

    # ---- helpers ------------------------------------------------------- #

    def _invoice_header(self, invoice_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT i.*,
                          CASE WHEN i.type='sale' THEN c.name ELSE s.name END AS party_name
                   FROM invoices i
                   LEFT JOIN customers c ON c.id=i.customer_id
                   LEFT JOIN suppliers s ON s.id=i.supplier_id
                   WHERE i.id=?""",
                (int(invoice_id),),
            ).fetchone()
            return dict(row) if row else None