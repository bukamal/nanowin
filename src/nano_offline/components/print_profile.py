from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

import flet as ft

from nano_offline.components import SegmentOption, SegmentedToggle
from nano_offline.core import barcode_settings
from nano_offline.core.theme import Colors

PrintAction = Callable[[str, int], Awaitable[None]]
PreviewHtml = Callable[[str, int], str | Awaitable[str]]

#: Last confirmed profile, reused by prints that run without a dialog
#: (e.g. POS "print automatically after payment") so they never interrupt
#: the cashier with a paper-type prompt on every sale.
PRINT_LAYOUT_KEY = "print_layout"
PRINT_ROLL_WIDTH_KEY = "print_roll_width"
DEFAULT_PRINT_LAYOUT = "sheet"
DEFAULT_PRINT_ROLL_WIDTH = "80"

# Per-surface overrides. The original global keys stay as the fallback, so
# existing installs keep their last confirmed choice until they print from a
# specific surface again. Keeping each surface independent means a cashier can
# keep POS on 80mm thermal while invoices stay on A4, without re-choosing on
# every print.
_SURFACE_KEYS: dict[str, tuple[str, str]] = {
    "invoice": ("print_layout_invoice", "print_roll_width_invoice"),
    "pos": ("print_layout_pos", "print_roll_width_pos"),
    "statement": ("print_layout_statement", "print_roll_width_statement"),
    "report": ("print_layout_report", "print_roll_width_report"),
    "voucher": ("print_layout_voucher", "print_roll_width_voucher"),
    "party": ("print_layout_party", "print_roll_width_party"),
    "item": ("print_layout_item", "print_roll_width_item"),
    "label": ("print_layout_label", "print_roll_width_label"),
    "dashboard": ("print_layout_dashboard", "print_roll_width_dashboard"),
}


def _profile_keys(surface: str) -> tuple[str, str]:
    return _SURFACE_KEYS.get(str(surface or "default"), (PRINT_LAYOUT_KEY, PRINT_ROLL_WIDTH_KEY))


def _layout_value(raw) -> str | None:
    value = str(raw or "").strip()
    return value if value in {"sheet", "roll"} else None


def _width_value(raw) -> str | None:
    value = str(raw or "").strip()
    return value if value in barcode_settings.LABEL_ROLL_WIDTH_MM else None


def saved_print_profile(settings, surface: str = "default") -> tuple[str, int]:
    """Return ``(layout, roll_width_mm)`` for ``surface``.

    Surface-specific values win; the original global keys are used as a
    fallback so upgrades never lose the user's previous choice.
    """
    layout_key, width_key = _profile_keys(surface)
    layout = _layout_value(settings.get(layout_key))
    if layout is None and layout_key != PRINT_LAYOUT_KEY:
        layout = _layout_value(settings.get(PRINT_LAYOUT_KEY))
    if layout is None:
        layout = DEFAULT_PRINT_LAYOUT

    width_value = _width_value(settings.get(width_key))
    if width_value is None and width_key != PRINT_ROLL_WIDTH_KEY:
        width_value = _width_value(settings.get(PRINT_ROLL_WIDTH_KEY))
    if width_value is None:
        width_value = DEFAULT_PRINT_ROLL_WIDTH
    return layout, barcode_settings.LABEL_ROLL_WIDTH_MM[width_value]


def save_print_layout(settings, surface: str, layout: str) -> None:
    """Persist only the layout choice for one surface (e.g. POS quick toggle)."""
    layout_key, _ = _profile_keys(surface)
    settings.set(layout_key, _layout_value(layout) or DEFAULT_PRINT_LAYOUT)


def _save_print_profile(settings, surface: str, layout: str, width_key: str) -> None:
    layout_setting, width_setting = _profile_keys(surface)
    settings.set(layout_setting, _layout_value(layout) or DEFAULT_PRINT_LAYOUT)
    settings.set(width_setting, _width_value(width_key) or DEFAULT_PRINT_ROLL_WIDTH)


def _test_page_html() -> str:
    """A small alignment page used by the dialog's printer-test button."""
    rows = "".join(
        f"<tr><td>مادة تجريبية {i}</td><td>2</td><td>10.00</td><td>20.00</td></tr>"
        for i in range(1, 4)
    )
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<style>
body {{ font-family:'Segoe UI',Tahoma,Arial,sans-serif; color:#1E293B; margin:0; font-size:12px; }}
.sheet {{ padding:8px; }}
.header {{ border-bottom:2px solid #0B5FCC; padding-bottom:8px; margin-bottom:12px; }}
h1 {{ margin:0; color:#0B5FCC; font-size:18px; }}
p {{ margin:4px 0 0; color:#64748B; }}
table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
th {{ background:#0B5FCC; color:#fff; padding:6px; font-size:11px; }}
td {{ padding:6px; border-bottom:1px solid #E2E8F0; text-align:center; }}
th:first-child, td:first-child {{ text-align:start; }}
.totals-wrap {{ width:58%; margin-inline-start:auto; margin-top:0; }}
.totals-table {{ width:100%; border:1px solid #E2E8F0; border-top:0; border-collapse:collapse; }}
.totals-table td {{ text-align:start; }}
.totals-table td:last-child {{ text-align:end; }}
.totals-table .total-row td {{ background:#EAF3FF; color:#0B5FCC; font-weight:800; }}
</style>
</head>
<body><div class="sheet">
<div class="header">
  <h1>صفحة اختبار الطباعة</h1>
  <p>تحقق من المحاذاة، حجم الخط، واتجاه RTL على الورق المحدد.</p>
</div>
<table>
<thead><tr><th>المادة</th><th>الكمية</th><th>السعر</th><th>الإجمالي</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<div class="totals-wrap"><table class="totals-table"><tbody>
<tr class="total-row"><td>الإجمالي</td><td>60.00</td></tr>
<tr><td>المدفوع</td><td>60.00</td></tr>
<tr><td>المتبقي</td><td>0.00</td></tr>
</tbody></table></div>
</div></body></html>"""


async def preview_document(
    native_files,
    html: str,
    *,
    name: str,
    layout: str,
    roll_width_mm: int,
) -> str:
    """Open a real preview of ``html`` for the selected paper profile.

    Returns ``"browser"`` when the desktop fallback opened it in the browser,
    or ``"shared"`` when a native PDF was handed to the share/view sheet.
    """
    if native_files is None:
        raise RuntimeError("المعاينة غير مهيأة في هذا البناء")
    pdf = await native_files.create_pdf(
        apply_print_profile(html, layout, roll_width_mm),
        filename=f"{name}-preview.pdf",
        paper_layout=layout,
        roll_width_mm=roll_width_mm,
    )
    if str(pdf).strip() == "browser":
        return "browser"
    await native_files.share_file(
        str(pdf),
        mime_type="application/pdf",
        subject="معاينة المستند",
    )
    return "shared"


def open_print_profile_dialog(
    page: ft.Page,
    settings,
    on_confirm: PrintAction,
    *,
    title: str = "إعدادات الطباعة",
    initial_layout: str | None = None,
    initial_roll_width: str | None = None,
    surface: str = "default",
    native_files=None,
    preview_builder: PreviewHtml | None = None,
) -> None:
    """Ask for A4/thermal layout before handing a document to the OS printer.

    When ``native_files`` is supplied, the dialog can print a built-in
    alignment page. When ``preview_builder`` is also supplied, it can render a
    real preview of the current document through the native PDF/browser path.
    """
    saved_layout, saved_width = saved_print_profile(settings, surface)
    layout = SegmentedToggle(
        options=[
            SegmentOption("sheet", "ورق A4", ft.Icons.DESCRIPTION_OUTLINED),
            SegmentOption("roll", "طابعة حرارية", ft.Icons.RECEIPT_LONG_OUTLINED),
        ],
        value=initial_layout if initial_layout in {"sheet", "roll"} else saved_layout,
    )
    width = SegmentedToggle(
        options=[SegmentOption(key, label) for key, label in barcode_settings.LABEL_ROLL_WIDTH_LABELS.items()],
        value=(
            initial_roll_width
            if initial_roll_width in barcode_settings.LABEL_ROLL_WIDTH_MM
            else str(saved_width)
        ),
    )
    width_row = ft.Row(
        [ft.Text("عرض اللفة", size=12, color=Colors.TEXT_SECONDARY), width],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        visible=layout.value == "roll",
    )
    hint = ft.Text(size=11, color=Colors.TEXT_SECONDARY)
    status = ft.Text("", size=10.5, color=Colors.TEXT_SECONDARY)

    def refresh(*, update: bool = True) -> None:
        is_roll = layout.value == "roll"
        width_row.visible = is_roll
        hint.value = f"تخطيط مناسب للفة بعرض {width.value} مم." if is_roll else "تخطيط صفحة A4 للطباعة المكتبية."
        if update:
            page.update()

    def selection() -> tuple[str, str, int]:
        selected_layout = layout.value or DEFAULT_PRINT_LAYOUT
        width_key = width.value or barcode_settings.DEFAULT_LABEL_ROLL_WIDTH
        roll_width_mm = barcode_settings.LABEL_ROLL_WIDTH_MM.get(
            width_key,
            barcode_settings.LABEL_ROLL_WIDTH_MM[barcode_settings.DEFAULT_LABEL_ROLL_WIDTH],
        )
        return selected_layout, width_key, roll_width_mm

    layout.on_change = lambda _: refresh()
    width.on_change = lambda _: refresh()
    refresh(update=False)

    dialog = ft.AlertDialog(modal=True)

    async def confirm(_=None) -> None:
        selected_layout, width_key, roll_width_mm = selection()
        try:
            _save_print_profile(settings, surface, selected_layout, width_key)
        except Exception:
            pass
        page.close(dialog)
        await asyncio.sleep(0.1)
        await on_confirm(selected_layout, roll_width_mm)

    async def preview(_=None) -> None:
        if native_files is None or preview_builder is None:
            return
        selected_layout, _, roll_width_mm = selection()
        status.value = "جارٍ تجهيز المعاينة..."
        status.color = Colors.TEXT_SECONDARY
        page.update()
        try:
            html = preview_builder(selected_layout, roll_width_mm)
            if inspect.isawaitable(html):
                html = await html
            result = await preview_document(
                native_files,
                str(html),
                name=f"nano-preview-{surface}",
                layout=selected_layout,
                roll_width_mm=roll_width_mm,
            )
            status.value = (
                "تم فتح المعاينة في المتصفح."
                if result == "browser"
                else "تم تجهيز ملف المعاينة."
            )
            status.color = Colors.SUCCESS_DARK
        except Exception as exc:
            status.value = f"تعذرت المعاينة: {exc}"
            status.color = Colors.DANGER
        page.update()

    async def test_print(_=None) -> None:
        if native_files is None:
            return
        selected_layout, _, roll_width_mm = selection()
        status.value = "جارٍ إرسال صفحة الاختبار..."
        status.color = Colors.TEXT_SECONDARY
        page.update()
        try:
            await native_files.print_html(
                apply_print_profile(_test_page_html(), selected_layout, roll_width_mm),
                name="nano-test-print",
                paper_layout=selected_layout,
                roll_width_mm=roll_width_mm,
            )
            status.value = "تم إرسال صفحة الاختبار إلى الطابعة."
            status.color = Colors.SUCCESS_DARK
        except Exception as exc:
            status.value = f"تعذرت الطباعة التجريبية: {exc}"
            status.color = Colors.DANGER
        page.update()

    dialog.title = ft.Text(title)
    dialog.content = ft.Column(
        [
            ft.Text("اختر تنسيق الورق قبل متابعة الطباعة."),
            layout,
            width_row,
            hint,
            ft.Text(
                "يُحفظ الاختيار لهذه الواجهة فقط، ويُستخدم تلقائيًا في المرة القادمة.",
                size=10.5,
                color=Colors.TEXT_FAINT,
            ),
            status,
        ],
        tight=True,
        spacing=12,
    )
    actions = [ft.TextButton("إلغاء", on_click=lambda _: page.close(dialog))]
    if native_files is not None and preview_builder is not None:
        actions.append(
            ft.TextButton(
                "معاينة",
                icon=ft.Icons.VISIBILITY_OUTLINED,
                on_click=preview,
            )
        )
    if native_files is not None:
        actions.append(
            ft.TextButton(
                "اختبار الطابعة",
                icon=ft.Icons.PRINT_OUTLINED,
                on_click=test_print,
            )
        )
    actions.append(
        ft.FilledButton("متابعة للطباعة", icon=ft.Icons.PRINT_OUTLINED, on_click=confirm)
    )
    dialog.actions = actions
    page.open(dialog)


def apply_print_profile(html: str, layout: str, roll_width_mm: int) -> str:
    """Override document page geometry for the selected paper profile."""
    if layout == "roll":
        width_mm = int(roll_width_mm)
        # Thermal roll: the page width must equal the paper width (a full-bleed
        # Letter page only printed a blank sliver), AND the content must stay
        # inside the *printable* area -- an 80mm roll has a wider (often
        # asymmetric) non-printable band, so a body forced to the full 80mm had
        # its RTL start (the right edge) clipped by the head. We keep the page
        # 80mm but give it symmetric page margins so the content column lands
        # inside the printable band. ``size: 80mm auto`` is invalid CSS
        # (Chromium drops it) so we seed a safe height and let ``fit()`` replace
        # it with the real content height before printing. Text is forced to
        # solid black at a slightly larger size, and coloured fills (blue table
        # header, tinted totals, badges) are neutralised to black-on-white: the
        # base slate (#1E293B) plus 9px anti-aliased glyphs rasterise through
        # pdfium and print faint on a thermal head, and a large coloured fill
        # prints as an uneven grey wash that swallows white header text.
        # 6mm still lost ~2 characters on this 80mm printer, so 12mm now leaves
        # roughly five more characters of clearance (56mm content column).
        side_mm = 12
        printable_mm = max(20, width_mm - 2 * side_mm)
        style = f"""
<style id="nano-print-profile">
@page {{ size: {width_mm}mm 297mm; margin: 0 {side_mm}mm; }}
html {{ width: {printable_mm}mm !important; min-width: 0 !important; max-width: {printable_mm}mm !important; margin: 0 auto !important; padding: 0 !important; }}
body {{ width: 100% !important; max-width: 100% !important; margin: 0 auto !important; padding: 0 !important; }}
body {{ color: #000 !important; font-size: 11px !important; line-height: 1.5 !important; font-weight: 500 !important; -webkit-print-color-adjust: exact !important; }}
.sheet {{ width: 100% !important; max-width: 100% !important; padding: 0 !important; }}
.header {{ display: block !important; padding: 8px !important; margin-bottom: 8px !important; border-radius: 5px !important; }}
.logo {{ max-width: 44px !important; height: 34px !important; }}
.brand {{ font-size: 18px !important; color: #000 !important; }}
.brand-meta {{ font-size: 10px !important; gap: 2px 6px !important; color: #111 !important; }}
.brand-meta b {{ color: #000 !important; }}
.doc-title {{ font-size: 15px !important; color: #000 !important; }}
.doc-subtitle {{ color: #111 !important; }}
.doc-number {{ font-size: 10px !important; padding: 3px 6px !important; }}
.meta-row {{ gap: 4px 8px !important; margin-bottom: 6px !important; font-size: 10px !important; color: #111 !important; }}
.meta-row b {{ color: #000 !important; }}
.meta-row .sep {{ color: #999 !important; }}
.badge {{ padding: 2px 6px !important; font-size: 10px !important; }}
.rate-note {{ margin-bottom: 6px !important; padding: 4px 7px !important; font-size: 10px !important; color: #111 !important; }}
.rate-note b {{ color: #000 !important; }}
.grid, .metrics {{ display: block !important; }}
.metric {{ margin: 4px 0 !important; padding: 6px !important; }}
.metric small {{ color: #111 !important; }}
.metric strong {{ color: #000 !important; }}
table {{ width: 100% !important; table-layout: fixed !important; font-size: 10px !important; }}
th, td {{ padding: 5px 4px !important; overflow-wrap: anywhere !important; }}
.inv-table .item-cell {{ font-size: 10px !important; color: #000 !important; }}
.inv-table .unit, .inv-table .equiv {{ font-size: 9px !important; color: #111 !important; }}
.totals-wrap {{ width: 100% !important; margin: 0 !important; }}
.totals-table {{ border: 0 !important; border-radius: 0 !important; }}
.totals-table td {{ padding: 4px 3px !important; font-size: 10px !important; color: #000 !important; }}
.totals-table td:first-child {{ color: #000 !important; }}
.totals-table .total-row td, .totals-table .remaining-row td, .totals-table .settled-row td {{ font-size: 11px !important; }}
.totals-table .fx {{ color: #111 !important; }}
.note {{ margin-top: 6px !important; padding: 5px 6px !important; border-radius: 5px !important; font-size: 10px !important; color: #000 !important; }}
.note b {{ color: #000 !important; }}
.footer {{ margin-top: 8px !important; padding-top: 5px !important; font-size: 9px !important; color: #111 !important; }}
.sign-row {{ margin-top: 8px !important; }}
.verify-box svg {{ width: 46px !important; height: 46px !important; }}
.verify-box {{ color: #111 !important; }}
.verify-token b {{ font-size: 10px !important; color: #000 !important; }}
.progress-wrap {{ margin-bottom: 6px !important; }}
.progress-label {{ color: #111 !important; }}
.watermark {{ font-size: 34px !important; }}
.header {{ background: #fff !important; border: 1px solid #000 !important; border-top: 4px solid #000 !important; }}
thead th {{ background: #fff !important; color: #000 !important; border-bottom: 2px solid #000 !important; border-inline-end: 1px solid #999 !important; font-weight: 700 !important; }}
thead th:first-child, thead th:last-child {{ border-radius: 0 !important; }}
thead th:last-child {{ border-inline-end: 0 !important; }}
tbody tr:nth-child(even) td {{ background: #fff !important; }}
td, .totals-table td {{ border-bottom: 1px solid #999 !important; }}
tbody tr:last-child td {{ border-bottom: 1px solid #999 !important; }}
.doc-number {{ background: #fff !important; color: #000 !important; border: 1px solid #000 !important; }}
.badge {{ background: #fff !important; color: #000 !important; border: 1px solid #000 !important; }}
.totals-table .total-row td {{ background: #fff !important; color: #000 !important; border-top: 2px solid #000 !important; }}
.totals-table .remaining-row td, .totals-table .settled-row td {{ background: #fff !important; color: #000 !important; }}
.note {{ background: #fff !important; border: 1px solid #000 !important; }}
.rate-note {{ background: #fff !important; border: 1px dashed #666 !important; }}
.metric {{ background: #fff !important; border: 1px solid #999 !important; }}
.metric::before {{ background: #000 !important; }}
.progress-track {{ background: #fff !important; border: 1px solid #999 !important; }}
.progress-fill {{ background: #000 !important; }}
</style>
<script id="nano-print-size">
(function () {{
  var MM = {width_mm};
  var SIDE = {side_mm};
  function fit() {{
    try {{
      var sheet = document.querySelector('.sheet') || document.body;
      var px = Math.max(
        sheet ? sheet.scrollHeight : 0,
        document.body ? document.body.scrollHeight : 0,
        document.documentElement ? document.documentElement.scrollHeight : 0
      );
      var mm = Math.ceil((px * 25.4) / 96) + 4;
      if (!isFinite(mm) || mm < 40) mm = 40;
      if (mm > 2000) mm = 2000;
      var node = document.getElementById('nano-page-size');
      if (!node) {{
        node = document.createElement('style');
        node.id = 'nano-page-size';
        document.head.appendChild(node);
      }}
      node.textContent = '@page {{ size: ' + MM + 'mm ' + mm + 'mm; margin: 0 ' + SIDE + 'mm; }}';
    }} catch (e) {{}}
  }}
  if (document.readyState === 'complete') {{ fit(); }}
  else {{ window.addEventListener('load', fit); }}
  setTimeout(fit, 400);
}})();
</script>
"""
    else:
        style = """
<style id="nano-print-profile">
@page { size: A4; margin: 12mm; }
html, body { width: auto !important; min-width: 0 !important; max-width: 100% !important; margin: 0 !important; padding: 0 !important; }
.sheet { width: 100% !important; max-width: 100% !important; }
</style>
"""

    marker = "</head>"
    if marker in html.lower():
        head_end = html.lower().index(marker)
        return html[:head_end] + style + html[head_end:]
    html_end = html.lower().rfind("</html>")
    if html_end >= 0:
        return html[:html_end] + "<head>" + style + "</head>" + html[html_end:]
    return "<!doctype html><html><head>" + style + "</head><body>" + html + "</body></html>"
