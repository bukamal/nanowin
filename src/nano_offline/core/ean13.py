"""Offline EAN-13 (and UPC-A) barcode renderer -> inline SVG.

UPC-A is encoded as EAN-13 with a leading ``0`` (the checksum is identical),
so one renderer covers both. Only codes whose mod-10 check digit is valid are
rendered; the label printer falls back to Code128 otherwise, so a bad digit is
never printed as a (wrong) EAN.
"""

from __future__ import annotations

from nano_offline.core.barcode_quality import _ean_upc_checksum_ok

# EAN-13 digit encodings, 7 modules each.
_L = {
    0: "0001101", 1: "0011001", 2: "0010011", 3: "0111101", 4: "0100011",
    5: "0110001", 6: "0101111", 7: "0111011", 8: "0110111", 9: "0001011",
}
_G = {
    0: "0100111", 1: "0110011", 2: "0011011", 3: "0100001", 4: "0011101",
    5: "0111001", 6: "0000101", 7: "0010001", 8: "0001001", 9: "0010111",
}
_R = {
    0: "1110010", 1: "1100110", 2: "1101100", 3: "1000010", 4: "1011100",
    5: "1001110", 6: "1010000", 7: "1000100", 8: "1001000", 9: "1110100",
}
# Left-half parity chosen by the leading digit.
_PARITY = (
    "LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
    "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL",
)


def ean13_modules(code: str) -> str | None:
    """Return the 95-module bit string (1=bar) for a valid EAN-13/UPC-A code,
    or ``None`` when the code is not 12/13 numeric digits with a good check."""
    text = (code or "").strip()
    if len(text) == 12:          # UPC-A -> EAN-13
        text = "0" + text
    if len(text) != 13 or not text.isdigit():
        return None
    if not _ean_upc_checksum_ok(text):
        return None
    bits = "101"
    parity = _PARITY[int(text[0])]
    for i, ch in enumerate(text[1:7]):
        bits += (_L if parity[i] == "L" else _G)[int(ch)]
    bits += "01010"
    for ch in text[7:]:
        bits += _R[int(ch)]
    bits += "101"
    return bits


def ean13_svg(code: str, *, width: int = 210, height: int = 54, show_text: bool = True, bar_color: str = "#0F172A") -> str:
    """Render ``code`` as an EAN-13/UPC-A SVG, or ``""`` if it is not valid."""
    modules = ean13_modules(code)
    if modules is None:
        return ""
    total = len(modules)
    unit = width / total if total else 1.0
    text_h = 12 if show_text else 0
    bar_h = max(6, height - text_h)
    rects: list[str] = []
    i = 0
    while i < total:
        if modules[i] == "1":
            j = i
            while j < total and modules[j] == "1":
                j += 1
            rects.append(
                f'<rect x="{i * unit:.3f}" y="0" width="{(j - i) * unit:.3f}" '
                f'height="{bar_h}" fill="{bar_color}"/>'
            )
            i = j
        else:
            i += 1
    label = str(code or "").strip()
    text_svg = (
        f'<text x="{width / 2:.2f}" y="{height - 2}" text-anchor="middle" '
        f'font-family="monospace" font-size="11" fill="{bar_color}">{label}</text>'
        if show_text else ""
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
        f"{''.join(rects)}{text_svg}</svg>"
    )


__all__ = ["ean13_svg", "ean13_modules"]