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
import time

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

    def enqueue_invoice(self, invoice_id: int, *, only_new: bool = False, dispatch: bool | None = None) -> int:
        """Create pending print jobs for a sale invoice's sections.

        Returns the number of *new* jobs. A no-op when the invoice is not a
        sale or no line resolves to an active, configured section. With
        ``only_new`` only lines not yet sent are printed (incremental kitchen
        sends after items are added to an open order).
        """
        invoice = self._invoice_header(invoice_id)
        if invoice is None or str(invoice.get("type")) != "sale":
            return 0
        grouped = self.sections.invoice_section_lines(int(invoice_id), only_unsent=only_new)
        if not grouped:
            return 0
        enqueued = 0
        printed_line_ids: list[int] = []
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
            line_ids = [int(line["line_id"]) for line in lines]
            batch = "-".join(str(i) for i in sorted(line_ids))
            if self.sections.enqueue_job(
                invoice_id=int(invoice_id),
                section_id=int(section_id),
                printer_host=host,
                printer_port=int(section.get("printer_port") or 9100),
                payload=payload,
                dedupe_key=f"invoice:{int(invoice_id)}:section:{int(section_id)}:batch:{batch}",
            ):
                enqueued += 1
            printed_line_ids.extend(line_ids)
        if printed_line_ids:
            # Optimistic: a line is considered "sent" once queued; a failed
            # job is recovered via the full "resend all tickets" action.
            self.sections.mark_lines_printed(printed_line_ids)
        should_dispatch = self.auto_dispatch if dispatch is None else dispatch
        if enqueued and should_dispatch:
            self.dispatch_async()
        return enqueued

    def send_new_invoice(self, invoice_id: int) -> int:
        """Send only the lines added since the order was last sent to kitchen."""
        return self.enqueue_invoice(int(invoice_id), only_new=True)

    def enqueue_void_diffs(
        self,
        invoice_id: int,
        before_lines: list[dict],
        after_lines: list[dict],
        *,
        dispatch: bool | None = None,
    ) -> int:
        """Print a cancellation ticket for lines removed/reduced on an edit.

        Lines are matched by (item, description); a quantity drop on any
        match is voided. Only sections with a configured printer receive one.
        """
        invoice = self._invoice_header(invoice_id)
        if invoice is None or str(invoice.get("type")) != "sale":
            return 0

        def key_of(line: dict) -> tuple:
            return (line.get("item_id"), str(line.get("description") or ""))

        old_qty: dict[tuple, float] = {}
        for line in before_lines:
            old_qty[key_of(line)] = old_qty.get(key_of(line), 0.0) + float(line.get("quantity") or 0)
        new_qty: dict[tuple, float] = {}
        for line in after_lines:
            new_qty[key_of(line)] = new_qty.get(key_of(line), 0.0) + float(line.get("quantity") or 0)
        voids = [
            (item_key, old_qty[item_key] - new_qty.get(item_key, 0.0))
            for item_key in old_qty
            if old_qty[item_key] - new_qty.get(item_key, 0.0) > 1e-9
        ]
        if not voids:
            return 0

        grouped: dict[int, list[dict]] = {}
        for (item_id, description), qty in voids:
            section_id = self.sections.section_for_item(item_id) if item_id is not None else None
            if section_id is None:
                continue
            section = self.sections.get(section_id)
            if section is None or not int(section.get("active") or 0):
                continue
            if not str(section.get("printer_host") or "").strip():
                continue
            grouped.setdefault(section_id, []).append({"name": description, "quantity": qty})

        enqueued = 0
        stamp = int(time.time() * 1000)
        for section_id, lines in grouped.items():
            section = self.sections.get(section_id)
            if section is None:
                continue
            try:
                payload = escpos.build_void_ticket(section, invoice, lines)
            except Exception:
                _LOG.exception("failed to build void ticket for section %s", section_id)
                continue
            if self.sections.enqueue_job(
                invoice_id=int(invoice_id),
                section_id=int(section_id),
                printer_host=str(section.get("printer_host")),
                printer_port=int(section.get("printer_port") or 9100),
                payload=payload,
                dedupe_key=f"invoice:{int(invoice_id)}:void:{int(section_id)}:{stamp}",
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
        attempted: set[int] = set()

        def _attempt(job: dict) -> None:
            job_id = int(job["id"])
            attempted.add(job_id)
            try:
                send(
                    str(job["printer_host"]),
                    int(job["printer_port"] or 9100),
                    bytes(job["payload"]),
                )
            except Exception as exc:  # noqa: BLE001 - any network error is a retry
                attempts = int(job.get("attempts") or 0) + 1
                final = attempts >= MAX_ATTEMPTS
                self.sections.mark_failed(job_id, str(exc), final=final)
                result["failed" if final else "pending"] += 1
                if final:
                    self._try_fallback(job)
            else:
                self.sections.mark_sent(job_id)
                result["sent"] += 1

        for job in self.sections.pending_jobs(limit=limit):
            _attempt(job)
        # A job that just permanently failed may have queued its section's
        # fallback printer above; send those now without re-attempting any job
        # already handled in this pass.
        for job in self.sections.pending_jobs(limit=limit):
            if int(job["id"]) in attempted:
                continue
            _attempt(job)
        return result

    def _try_fallback(self, job: dict) -> None:
        """Re-queue a permanently failed job to its section's fallback printer."""
        if ":fallback:" in str(job.get("dedupe_key") or ""):
            return  # never chain fallbacks (prevents loops)
        fallback = self.sections.fallback_for(job.get("section_id"))
        if fallback is None or not int(fallback.get("active") or 0):
            return
        host = str(fallback.get("printer_host") or "").strip()
        if not host:
            return
        self.sections.enqueue_job(
            invoice_id=int(job["invoice_id"]),
            section_id=int(fallback["id"]),
            printer_host=host,
            printer_port=int(fallback.get("printer_port") or 9100),
            payload=bytes(job["payload"]),
            dedupe_key=f"invoice:{int(job['invoice_id'])}:fallback:{int(job['id'])}",
        )

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