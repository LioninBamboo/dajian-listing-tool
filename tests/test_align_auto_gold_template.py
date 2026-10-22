from scripts.align_auto_descriptions import (
    PublishedListing,
    build_candidate,
    looks_like_gold_template,
    validate_description,
)


def _listing(**kwargs):
    return PublishedListing(
        sku=kwargs.get("sku", "W2801P516393"),
        listing_id=kwargs.get("listing_id", "188777275513"),
        title=kwargs.get(
            "title",
            "Door and Hard Top Storage Cart for 2007-2026 Jeep Wrangler JK JL",
        ),
        source_description=kwargs.get(
            "source_description",
            "Steel storage cart for Jeep Wrangler hardtop and doors. Includes extra storage box and MOLLE panel.",
        ),
        attributes=kwargs.get("attributes", {"Material": "Steel"}),
        specs=kwargs.get("specs", {"Assembled Length (in.)": "27.2", "Assembled Width (in.)": "15.3", "Assembled Height (in.)": "7.3"}),
        aspects=kwargs.get(
            "aspects",
            {
                "Type": ["Storage Cart"],
                "Material": ["Steel"],
                "Finish": ["Black"],
                "Fitment Type": ["Vehicle Specific Fit"],
                "Item Length": ["27.2 in"],
                "Item Width": ["15.3 in"],
                "Item Height": ["7.3 in"],
            },
        ),
    )


def test_furniture_decor_description_is_not_gold():
    furniture = (
        "<div>AQUARIDES KEY FEATURES SPECIFICATIONS PACKAGE INCLUDES"
        " Finished in steel, this Storage Cart brings a polished look"
        " without overwhelming the room and pairs easily with modern or classic décor."
        " Ships from US Warehouse</div>"
    )
    assert looks_like_gold_template(furniture) is False
    assert "contains_furniture_decor" in validate_description(furniture)["errors"]


def test_build_candidate_emits_gold_attribute_table():
    candidate = build_candidate(_listing())
    desc = candidate.description
    assert candidate.validation["passed"], candidate.validation["errors"]
    assert looks_like_gold_template(desc)
    assert "#1f2329" in desc
    assert "Attribute" in desc
    assert "PACKAGE INCLUDES" in desc
    assert "overwhelming the room" not in desc.lower()
    assert "Assembly Required" not in desc
