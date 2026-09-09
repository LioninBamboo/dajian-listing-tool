from src.utils.conversion_copy import (
    build_conversion_feature_bullets,
    is_thin_key_features_description,
    _is_keyword_soup,
)


def test_keyword_soup_detection():
    assert _is_keyword_soup("armrest easy to assemble king size storage multi-purpose upholstered")
    assert not _is_keyword_soup(
        "Soft chenille upholstery delivers a plush hand-feel that elevates bedroom style."
    )


def test_conversion_bullets_are_real_sentences():
    bullets = build_conversion_feature_bullets(
        title='65" Ottoman Bench, Chenille Bench for Bedroom, End of Bed Bench Upholstered',
        source_description=(
            "This upholstered ottoman bench offers comfortable seating at the end of your bed. "
            "The soft chenille fabric is easy to maintain. Storage is available under the seat."
        ),
        attributes={"Main Material": "Chenille", "Main Color": "Beige"},
        specs={
            "Assembled Length (in.)": "65.4",
            "Assembled Width (in.)": "17.7",
            "Assembled Height (in.)": "21.6",
            "Product Weight (lbs.)": "31",
        },
        aspects={
            "Item Length": ["65.4 in"],
            "Item Width": ["17.7 in"],
            "Item Height": ["21.6 in"],
            "Material": ["chenille"],
            "Type": ["Ottoman"],
            "Assembly Required": ["No"],
        },
        characteristics=["armrest", "easy to assemble", "king size", "storage", "multi-purpose", "upholstered"],
        limit=6,
    )
    assert len(bullets) >= 4
    for b in bullets:
        assert len(b.split()) >= 6
        assert not _is_keyword_soup(b)
        assert b[0].isupper() or b[0].isdigit()


def test_thin_key_features_detector():
    thin = (
        "<div><h3>KEY FEATURES</h3><ul>"
        "<li>armrest easy to assemble king size storage multi-purpose upholstered</li>"
        "</ul></div>"
    )
    rich = (
        "<div><h3>KEY FEATURES</h3><ul>"
        "<li>Soft chenille upholstery delivers a plush hand-feel that elevates bedroom style.</li>"
        "<li>At about 65 inches long, it anchors the foot of a bed without overwhelming the room.</li>"
        "</ul></div>"
    )
    assert is_thin_key_features_description(thin)
    assert not is_thin_key_features_description(rich)
