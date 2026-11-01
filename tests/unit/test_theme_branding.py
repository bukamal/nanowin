from nano_offline.core import theme


def test_brand_accent_drives_light_and_dark_tokens_without_changing_status_colors():
    original_mode = theme.get_mode()
    original_accent = theme.get_brand_accent()
    try:
        theme.set_mode("light")
        light_success = theme.Colors.SUCCESS
        theme.set_brand_accent("#047857")
        assert theme.Colors.PRIMARY == "#047857"
        assert theme.Colors.PRIMARY_BG != theme.Colors.PRIMARY
        assert theme.Colors.SUCCESS == light_success

        light_primary = theme.Colors.PRIMARY
        theme.set_mode("dark")
        dark_success = theme.Colors.SUCCESS
        theme.set_brand_accent("#047857")
        assert theme.Colors.PRIMARY != light_primary
        assert theme.Colors.PRIMARY_BG != theme.Colors.BACKGROUND
        assert theme.Colors.SUCCESS == dark_success
    finally:
        theme.set_mode(original_mode)
        theme.set_brand_accent(original_accent)


def test_invalid_saved_brand_color_falls_back_to_cashir_blue():
    original_accent = theme.get_brand_accent()
    try:
        theme.set_brand_accent("#bad-value")
        assert theme.get_brand_accent() == "#0B5FCC"
    finally:
        theme.set_brand_accent(original_accent)


def test_brand_presets_are_all_valid_theme_colors():
    original_mode = theme.get_mode()
    original_accent = theme.get_brand_accent()
    try:
        assert len(theme.BRAND_PRESETS) >= 10
        for color, _label in theme.BRAND_PRESETS:
            theme.set_brand_accent(color)
            for mode in ("light", "dark"):
                theme.set_mode(mode)
                assert theme.Colors.PRIMARY
                assert theme.Colors.PRIMARY_BG
    finally:
        theme.set_mode(original_mode)
        theme.set_brand_accent(original_accent)
