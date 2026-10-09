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


class TestSupplierInternalCodesNeverReachBuyers:
    """2026-07-27 金丝雀验收:按源重建标题时,GIGA 留在品名里的退役编号
    被原样推到 eBay，3 条 live 标题变成
    "Full Size Murphy Bed with Large Drawers,Gray (OLD SKU:N708P336203E)"——
    真实搜索关键词被内部编号顶掉。全库 24 条源标题带此标记。"""

    def test_parenthesised_old_sku_is_removed(self):
        cleaned, changed = sanitize_listing_title(
            "Full Size Murphy Bed with Large Drawers,Gray (OLD SKU:N708P336203E)"
        )
        assert changed is True
        assert "OLD SKU" not in cleaned.upper()
        assert "N708P336203E" not in cleaned
        assert cleaned == "Full Size Murphy Bed with Large Drawers,Gray"

    def test_spaced_and_unparenthesised_variants(self):
        for raw in [
            "Queen Murphy Bed with Large Drawers,Green (OLD SKU: LP000678AAF)",
            "Twin Race Car Platform Bed with Wheels,Blue(OLD SKU:WF311965AAC)",
            "Console Table 63 in Long with Drawers OLD SKU: N715P372052",
            "Dining Set Espresso [ORIGINAL SKU: ABC123]",
        ]:
            cleaned, _ = sanitize_listing_title(raw)
            assert "SKU" not in cleaned.upper(), raw

    def test_normal_title_is_untouched(self):
        raw = '4 Pack Metal Garden Trellis 71" x 19.7" Rustproof Trellis for Climbing'
        cleaned, changed = sanitize_listing_title(raw)
        assert cleaned == raw
        assert changed is False

    def test_normalize_entrypoint_also_strips(self):
        out, _ = normalize_listing_title_for_ebay(
            "Queen Murphy Bed with Large Drawers,Green (OLD SKU: LP000678AAF)",
            source_title="Queen Murphy Bed with Large Drawers,Green (OLD SKU: LP000678AAF)",
        )
        assert "SKU" not in out.upper()

    def test_word_sku_alone_is_not_stripped(self):
        # 只清"OLD/NEW/ORIG/PREV SKU"这类簿记标记,不能误伤正常措辞
        cleaned, _ = sanitize_listing_title("Storage Bed Frame with Skudo Coating")
        assert "Skudo" in cleaned
