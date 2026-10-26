import asyncio

from nano_offline.components.print_profile import (
    apply_print_profile,
    preview_document,
    save_print_layout,
    saved_print_profile,
)


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


def test_thermal_profile_overrides_document_page_size_and_width():
    html = "<html><head><style>@page { size: A4; }</style></head><body><div>document</div></body></html>"

    result = apply_print_profile(html, "roll", 80)

    assert "@page { size: 80mm 297mm; margin: 0; }" in result
    assert "width: 80mm !important" in result
    assert result.index('id="nano-print-profile"') < result.lower().index("</head>")
    assert ".totals-wrap { width: 100% !important" in result
    # The injected style block must be closed: an unclosed <style> makes the
    # browser treat the rest of the document as CSS and print a blank page
    # (barcode labels worked only because they skip this wrapper).
    assert result.count("<style") == result.count("</style>")
    # The content-height autosize script must also be closed and present.
    assert result.count("<script") == result.count("</script>")
    assert "nano-page-size" in result


def test_a4_profile_overrides_preexisting_thermal_template():
    html = "<html><head><style>@page { size: 80mm auto; }</style></head><body>receipt</body></html>"

    result = apply_print_profile(html, "sheet", 80)

    assert "@page { size: A4; margin: 12mm; }" in result
    assert "max-width: 100% !important" in result
    assert result.count("<style") == result.count("</style>")


def test_profile_can_be_applied_to_html_without_head():
    result = apply_print_profile("<main>document</main>", "roll", 80)

    assert '<head>\n<style id="nano-print-profile">' in result
    assert "80mm 297mm" in result


def test_saved_print_profile_defaults_to_a4_sheet():
    assert saved_print_profile(_Settings()) == ("sheet", 80)


def test_saved_print_profile_returns_last_confirmed_choice():
    settings = _Settings(print_layout="roll", print_roll_width="80")

    assert saved_print_profile(settings) == ("roll", 80)


def test_saved_print_profile_ignores_invalid_values():
    settings = _Settings(print_layout="fax", print_roll_width="120")

    assert saved_print_profile(settings) == ("sheet", 80)


def test_saved_print_profile_prefers_surface_override():
    settings = _Settings(
        print_layout="sheet",
        print_roll_width="80",
        print_layout_pos="roll",
        print_roll_width_pos="80",
    )

    assert saved_print_profile(settings, "pos") == ("roll", 80)


def test_saved_print_profile_falls_back_to_global_for_new_surface():
    settings = _Settings(print_layout="roll", print_roll_width="80")

    assert saved_print_profile(settings, "invoice") == ("roll", 80)


def test_saved_print_profile_ignores_invalid_surface_override():
    settings = _Settings(
        print_layout="roll",
        print_roll_width="80",
        print_layout_pos="fax",
        print_roll_width_pos="120",
    )

    assert saved_print_profile(settings, "pos") == ("roll", 80)


def test_save_print_layout_is_scoped_to_surface():
    settings = _Settings()

    save_print_layout(settings, "pos", "roll")

    assert settings["print_layout_pos"] == "roll"
    assert "print_layout" not in settings


class _FakeNativeFiles:
    def __init__(self, pdf_result: str):
        self.pdf_result = pdf_result
        self.pdf_calls = []
        self.share_calls = []

    async def create_pdf(self, html, *, filename, paper_layout, roll_width_mm):
        self.pdf_calls.append((html, filename, paper_layout, roll_width_mm))
        return self.pdf_result

    async def share_file(self, path, *, mime_type, subject):
        self.share_calls.append((path, mime_type, subject))
        return True


def test_preview_document_uses_browser_fallback_without_sharing():
    native = _FakeNativeFiles("browser")

    result = asyncio.run(
        preview_document(
            native,
            "<html><head></head><body>doc</body></html>",
            name="nano-preview-invoice",
            layout="roll",
            roll_width_mm=80,
        )
    )

    assert result == "browser"
    assert native.share_calls == []
    assert native.pdf_calls[0][1] == "nano-preview-invoice-preview.pdf"
    assert native.pdf_calls[0][2:] == ("roll", 80)
    assert "80mm 297mm" in native.pdf_calls[0][0]


def test_preview_document_shares_pdf_on_native_platforms():
    native = _FakeNativeFiles("/tmp/preview.pdf")

    result = asyncio.run(
        preview_document(
            native,
            "<html><head></head><body>doc</body></html>",
            name="nano-preview-statement",
            layout="sheet",
            roll_width_mm=80,
        )
    )

    assert result == "shared"
    assert native.share_calls == [
        ("/tmp/preview.pdf", "application/pdf", "معاينة المستند")
    ]
    assert "@page { size: A4; margin: 12mm; }" in native.pdf_calls[0][0]
