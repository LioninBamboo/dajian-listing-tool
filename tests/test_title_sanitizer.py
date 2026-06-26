from src.utils.title_sanitizer import (
    normalize_listing_title_for_ebay,
    sanitize_listing_title,
    title_has_incomplete_trailing_fragment,
)


def test_sanitize_listing_title_removes_video_marker():
    cleaned, changed = sanitize_listing_title("[VIDEO] 110 Modular Sectional Sofa")

    assert changed is True
    assert cleaned == "110 Modular Sectional Sofa"


def test_sanitize_listing_title_removes_assembly_video_phrase_and_supplier_prefix():
    cleaned, changed = sanitize_listing_title(
        "[Assembly Video Provided] U_STYLE Modern 83.7in High Kitchen Pantry"
    )

    assert changed is True
    assert cleaned == "Modern 83.7in High Kitchen Pantry"


def test_title_truncation_detector_flags_incomplete_inch_fragment():
    live_title = "Mid Century TV Stand Media Console with 2 Drawers & Storage for TVs up to 85 Inc"
    source_title = "Mid Century TV Stand Media Console with 2 Drawers & Storage for TVs up to 85 Inch Natural"

    assert title_has_incomplete_trailing_fragment(live_title, source_title=source_title) is True


def test_normalize_listing_title_for_ebay_trims_broken_suffix_and_connector():
    title = "2 Burner Propane Gas Grill with Side Burner 24000 BTU Stainless Steel BBQ for Outdoor"

    cleaned, changed = normalize_listing_title_for_ebay(title)

    assert changed is True
    assert len(cleaned) <= 80
    assert cleaned == "2 Burner Propane Gas Grill with Side Burner 24000 BTU Stainless Steel BBQ"


def test_normalize_listing_title_for_ebay_removes_dangling_dimension_connector():
    live_title = "Extendable Dining Table with Folding Tabletop Bar Table 57.7 x 32.7"
    source_title = "Extendable Dining Table with Folding Tabletop Bar Table 57.7 x 32.7 x 37.8 in Black"

    cleaned, changed = normalize_listing_title_for_ebay(live_title, source_title=source_title)

    assert changed is True
    assert cleaned == "Extendable Dining Table with Folding Tabletop Bar Table 57.7"
