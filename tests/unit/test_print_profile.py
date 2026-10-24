from nano_offline.components.print_profile import apply_print_profile, saved_print_profile


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


def test_thermal_profile_overrides_document_page_size_and_width():
    html = "<html><head><style>@page { size: A4; }</style></head><body><div>document</div></body></html>"

    result = apply_print_profile(html, "roll", 58)

    assert "@page { size: 58mm auto; margin: 0; }" in result
    assert "width: 58mm !important" in result
    assert result.index('id="nano-print-profile"') < result.lower().index("</head>")
    # The injected style block must be closed: an unclosed <style> makes the
    # browser treat the rest of the document as CSS and print a blank page
    # (barcode labels worked only because they skip this wrapper).
    assert result.count("<style") == result.count("</style>")


def test_a4_profile_overrides_preexisting_thermal_template():
    html = "<html><head><style>@page { size: 80mm auto; }</style></head><body>receipt</body></html>"

    result = apply_print_profile(html, "sheet", 80)

    assert "@page { size: A4; margin: 12mm; }" in result
    assert "max-width: 100% !important" in result
    assert result.count("<style") == result.count("</style>")


def test_profile_can_be_applied_to_html_without_head():
    result = apply_print_profile("<main>document</main>", "roll", 80)

    assert '<head>\n<style id="nano-print-profile">' in result
    assert "80mm auto" in result


def test_saved_print_profile_defaults_to_a4_sheet():
    assert saved_print_profile(_Settings()) == ("sheet", 58)


def test_saved_print_profile_returns_last_confirmed_choice():
    settings = _Settings(print_layout="roll", print_roll_width="80")

    assert saved_print_profile(settings) == ("roll", 80)


def test_saved_print_profile_ignores_invalid_values():
    settings = _Settings(print_layout="fax", print_roll_width="120")

    assert saved_print_profile(settings) == ("sheet", 58)
