"""Minimal ESC/POS builder + raw network sender for station tickets.

The app targets network (Ethernet/Wi-Fi) thermal printers that accept raw
ESC/POS on TCP port 9100. Rendering is intentionally small and text-based:
Arabic is encoded with the section's configured code page (CP864 by default),
which the printer itself shapes. Per-section code page/width are configured in
the Aقسام والطابعات screen so each printer can be tuned independently.
"""

from __future__ import annotations

import socket

__all__ = ["build_station_ticket", "build_void_ticket", "test_ticket", "send_bytes", "columns_for_width"]

# ESC/POS control bytes.
_ESC = b"\x1b"
_GS = b"\x1d"
_INIT = _ESC + b"@"           # reset printer
_CODEPAGE = _ESC + b"t"       # select code page
_ALIGN = _ESC + b"a"          # 0 left / 1 center / 2 right
_BOLD = _ESC + b"E"           # 0/1
_SIZE = _ESC + b"!"           # char size flags
_FEED = b"\n"
_CUT = _GS + b"V" + b"\x00"   # full cut
_BEEP = _ESC + b"B"           # beep n times, duration

# Epson code-page numbers (the common mapping; override per printer if needed).
# CP1256 ("Windows Arabic") is the practical default for Arabic receipts on
# network thermal printers; CP864 is offered for printers that prefer it.
_CODE_PAGES = {
    "cp1256": 24,
    "cp864": 22,
    "cp437": 0,
    "cp850": 2,
    "cp858": 19,
}


def columns_for_width(width_mm: int) -> int:
    # ~32 chars on 58 mm, ~48 on 80 mm at the printer's default font.
    return 32 if int(width_mm or 80) <= 58 else 48


class _Builder:
    def __init__(self, *, encoding: str = "cp1256", width_mm: int = 80):
        self.encoding = (encoding or "cp864").lower()
        self.columns = columns_for_width(width_mm)
        self._parts: list[bytes] = []

    def raw(self, data: bytes) -> None:
        self._parts.append(data)

    def init(self) -> None:
        self.raw(_INIT)
        page = _CODE_PAGES.get(self.encoding)
        if page is not None:
            self.raw(_CODEPAGE + bytes([page]))
        self.raw(_BOLD + b"\x00")

    def align(self, where: int) -> None:
        self.raw(_ALIGN + bytes([where]))

    def bold(self, on: bool) -> None:
        self.raw(_BOLD + bytes([1 if on else 0]))

    def size(self, double_width: bool = False, double_height: bool = False) -> None:
        flags = (0x20 if double_width else 0) | (0x10 if double_height else 0)
        self.raw(_SIZE + bytes([flags]))

    def line(self, text: str = "") -> None:
        self.raw(self._encode(text) + _FEED)

    def wrapped(self, text: str, *, indent: str = "") -> None:
        for line in _wrap(text, self.columns - len(indent)):
            self.line(f"{indent}{line}")

    def divider(self, char: str = "-") -> None:
        self.line(char * self.columns)

    def feed(self, n: int = 1) -> None:
        self.raw(_FEED * max(1, int(n)))

    def beep(self) -> None:
        self.raw(_BEEP + b"\x02\x03")

    def cut(self) -> None:
        self.raw(_CUT)

    def _encode(self, text: str) -> bytes:
        try:
            return str(text).encode(self.encoding, errors="replace")
        except LookupError:
            return str(text).encode("cp864", errors="replace")

    def build(self) -> bytes:
        return b"".join(self._parts)


def _wrap(text: str, width: int) -> list[str]:
    """Word-wrap by characters, preserving explicit newlines."""
    width = max(8, int(width))
    out: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        if not raw_line.strip():
            out.append("")
            continue
        current = ""
        for word in raw_line.split():
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current = f"{current} {word}"
            else:
                out.append(current)
                current = word
            while len(current) > width:
                out.append(current[:width])
                current = current[width:]
        out.append(current)
    return out or [""]


def _fmt_qty(value: float) -> str:
    qty = float(value or 0)
    return f"{qty:g}"


def build_station_ticket(
    section: dict,
    invoice: dict,
    lines: list[dict],
    *,
    copies: int | None = None,
) -> bytes:
    """Render one section's station ticket (no prices) to ESC/POS bytes."""
    b = _Builder(
        encoding=str(section.get("encoding") or "cp1256"),
        width_mm=int(section.get("paper_width_mm") or 80),
    )
    b.init()
    b.align(1)
    b.size(double_width=True, double_height=True)
    b.bold(True)
    b.line(str(section.get("name") or "قسم"))
    b.size()
    b.bold(False)
    b.align(0)
    b.divider("=")

    ref = str(invoice.get("reference") or "").strip()
    invoice_no = invoice.get("id")
    b.line(f"فاتورة: #{invoice_no}" + (f"  ({ref})" if ref else ""))
    date_text = str(invoice.get("invoice_date") or "").strip()
    if date_text:
        b.line(f"التاريخ: {date_text}")
    party = str(invoice.get("party_name") or "").strip()
    if party:
        b.line(f"الجهة: {party}")
    b.divider("-")

    for line in lines:
        name = str(line.get("name") or line.get("description") or "").strip()
        qty = _fmt_qty(line.get("quantity"))
        b.bold(True)
        b.wrapped(f"{qty} × {name}")
        b.bold(False)
        note = str(line.get("notes") or "").strip()
        if note:
            b.wrapped(note, indent="   ")

    b.divider("=")
    notes = str(invoice.get("notes") or "").strip()
    if notes:
        b.wrapped(notes)
        b.divider("-")
    b.align(1)
    b.line("تحضير")
    b.feed(2)
    b.cut()

    payload = b.build()
    repeat = max(1, int(copies if copies is not None else section.get("copies") or 1))
    return payload * repeat


def build_void_ticket(section: dict, invoice: dict, lines: list[dict], *, copies: int | None = None) -> bytes:
    """Render a kitchen cancellation ticket for the given voided lines."""
    b = _Builder(
        encoding=str(section.get("encoding") or "cp1256"),
        width_mm=int(section.get("paper_width_mm") or 80),
    )
    b.init()
    b.align(1)
    b.size(double_width=True, double_height=True)
    b.bold(True)
    b.line("إلغاء")
    b.size()
    b.bold(False)
    b.line(str(section.get("name") or "قسم"))
    b.align(0)
    b.divider("=")
    ref = str(invoice.get("reference") or "").strip()
    b.line(f"فاتورة: #{invoice.get('id')}" + (f"  ({ref})" if ref else ""))
    b.divider("-")
    for line in lines:
        name = str(line.get("name") or line.get("description") or "").strip()
        b.bold(True)
        b.wrapped(f"إلغاء {_fmt_qty(line.get('quantity'))} × {name}")
        b.bold(False)
    b.divider("=")
    b.feed(2)
    b.cut()

    payload = b.build()
    repeat = max(1, int(copies if copies is not None else section.get("copies") or 1))
    return payload * repeat


def test_ticket(section: dict) -> bytes:
    """A small sample ticket used by the screen's "test print" button."""
    return build_station_ticket(
        section,
        {
            "id": 0,
            "reference": "TEST",
            "invoice_date": "",
            "party_name": "",
            "notes": "طباعة تجريبية لطابعة القسم",
        },
        [{"name": "مادة تجريبية", "quantity": 1}],
        copies=1,
    )


def send_bytes(host: str, port: int = 9100, data: bytes = b"", *, timeout: float = 4.0) -> None:
    """Send raw ESC/POS bytes to a network printer and close the socket."""
    host = str(host or "").strip()
    if not host:
        raise ValueError("عنوان طابعة القسم غير محدد")
    with socket.create_connection((host, int(port or 9100)), timeout=timeout) as sock:
        sock.sendall(data)