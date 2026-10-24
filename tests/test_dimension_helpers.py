from src.utils.dimension_helpers import (
    extract_product_dimensions_from_text,
    find_weight,
)


def test_product_weight_takes_precedence_over_overall_product_weight():
    """The source's item weight must win when both labels are present."""
    attrs = {
        "Overall Product Weight": "106.52 lbs",
        "Product Weight (lbs.)": "102.52",
    }

    assert find_weight(attrs) == 102.52


def test_extracts_luggage_size_triplet_as_length_width_height():
    text = (
        '28-inch checked luggage size: 18.5*11.6*29.5". '
        "Ideal for long-distance trips."
    )

    assert extract_product_dimensions_from_text(text) == {
        "length": 18.5,
        "width": 11.6,
        "height": 29.5,
    }
