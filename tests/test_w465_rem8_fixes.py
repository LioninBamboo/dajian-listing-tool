"""Quick unit checks for rem8 publish blockers."""
from src.utils.listing_quality_gate import classify_listing_profile
from src.utils.listing_fact_sheet import compare_fact_sheets


def test_camping_kitchen_not_side_table():
    title = (
        "Folding Camping Kitchen Table - Portable Outdoor Cooking Station "
        "with Heat Resistant Top, 4 Side Tables, Storage Rack"
    )
    profile = classify_listing_profile(title, "", "")
    assert profile.kind == "camping_kitchen_table"
    assert profile.category_id == "112590"


def test_umbrella_foldable_supported_by_product_type():
    source = {
        "materials": ["polyester", "steel"],
        "features": ["waterproof", "tilt", "crank mechanism"],
        "counts": {},
        "capacity": None,
        "certifications": [],
        "dimensions": {},
    }
    live = dict(source, features=["foldable", "waterproof", "tilt"])
    title = "7.5 FT Patio Umbrella Outdoor Market Yard Umbrella"
    violations = compare_fact_sheets(
        source, live, live_text=title, source_text=title + " crank tilt waterproof"
    )
    foldable = [v for v in violations if v.get("claim_text") == "foldable"]
    assert foldable == [], foldable
