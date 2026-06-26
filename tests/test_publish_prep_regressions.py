from src.services.ebay_category_matcher import EbayCategoryMatcher
from src.utils.dimension_helpers import extract_product_dimensions_from_text


def test_dimension_parser_ignores_weight_block_after_overall_dimensions():
    text = (
        "Dimension(Overall) Island:53.14 x 29.52 x 36.4 inches "
        "Stools:15.7 x 10.8 x 25.6 inches "
        "Product Weight Island:128.2 LBS Stools:18.08 LBS "
        "Package Dimension Package1: 56.1 x 20.28 x 7.28"
    )

    assert extract_product_dimensions_from_text(text) == {
        "length": 53.1,
        "width": 29.5,
        "height": 36.4,
    }


def test_keyword_fallback_does_not_treat_adjustable_as_table():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Adjustable Weighted Vest for Women and Men 10 LBS Breathable Workout Vest"

    assert matcher.get_keyword_category_hint(title) != ("38204", "Tables")
    assert not matcher.is_category_plausible_for_text(title, "38204", "Tables")
