"""Bug 2: category_mismatch false positives from GIGA stuffing and substring markers."""

from __future__ import annotations

import json

import pytest

from src.utils.listing_quality_gate import classify_listing_profile


REAL_SKU_CASES = (
    (
        "W808P475145",
        "Kids 3 Drawer Dresser with Recessed Handle, Baby Nightstand for Bedroom, Storage Cabinet for Toddler",
        "DCW26010-S Sage Wood Nightstand with Anti-Tip Kit 31.5 x 11.8 x 28.9 in",
        "38199",
        {"nightstand", "unknown"},
        {"20487"},
    ),
    (
        "W46528009",
        "WOOD WORK BENCH 115cm",
        "Heavy Duty Steel Workbench with Pegboard & Drawer",
        "175752",
        {"unknown"},
        {"262980"},
    ),
    (
        "W3098P470272",
        '65.75" Wide Modern Upholstered Storage Bench With Double Lids, Foot Stool With Rolled Armrest, Bench Daybed',
        "65.75 Inch Wide Storage Ottoman Bench Upholstered Napped Fabric Double Sofa",
        "20490",
        {"storage_ottoman", "ottoman_bench", "indoor_bench"},
        {"38208"},
    ),
    (
        "N817P237961B",
        "Floral Accent Chair, Velvet Lounge Arm Chair with Solid Wood Frame, Upholstered Living Room Chairs Reading Chair for Balcony",
        "Velvet Accent Armchair with Solid Wood Frame and Metal Legs",
        "38208",
        {"armchair"},
        {"79684"},
    ),
    (
        "W2105P480697",
        "Leather Armchair, Modern Accent Chair High Back, Living Room Chairs Sofa Chairs for Home Office,Bedroom,Dining Room",
        "Modern Brown Faux Leather Accent Chair with Metal Frame, High Back Armchair",
        "38208",
        {"armchair"},
        {"54235"},
    ),
)


@pytest.mark.parametrize(
    "sku,giga,live,stored,allowed_kinds,forbidden_cats",
    REAL_SKU_CASES,
    ids=[row[0] for row in REAL_SKU_CASES],
)
def test_real_sku_category_false_positives(sku, giga, live, stored, allowed_kinds, forbidden_cats):
    profile = classify_listing_profile(live, "", stored, source_title=giga)
    assert profile.kind in allowed_kinds, (sku, profile.kind, profile.category_id)
    assert profile.category_id not in forbidden_cats
    concatenated = classify_listing_profile(f"{giga} {live}", "")
    assert concatenated.kind in allowed_kinds or concatenated.kind == "unknown"
    assert concatenated.category_id not in forbidden_cats


def test_negative_n719_outdoor_kitchen_island_not_remapped_to_wrong_family():
    title = "Outdoor Kitchen Island Fir Wood Prep Station Lockable Kitchen Cart"
    profile = classify_listing_profile(title, "", "177000")
    assert profile.kind not in {"pantry_cabinet", "indoor_bench", "outdoor_chair", "sofa", "dining_chair"}
    # Stored kitchen-island leaf stays unless a confident other family wins.
    if profile.category_id:
        assert profile.category_id != "20487"


def test_negative_w2275_curio_still_flagged_away_from_stale_leaf():
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Oauth:
        def get_application_token(self):
            return "offline"

    title = "Lighted Glass Curio Cabinet with Adjustable Shelves"
    profile = classify_listing_profile(title, "", "125085")
    assert profile.kind != "pantry_cabinet"
    canon, name = EbayCategoryMatcher(_Oauth()).canonicalize_category(title, "125085", None, "")
    assert canon not in {None, "", "125085"}
    assert canon != "20487"


@pytest.mark.parametrize(
    "title,expected_kind",
    [
        ("Kitchen Pantry Cabinet with Doors and Shelves", "pantry_cabinet"),
        ("Upholstered Dining Bench with Storage", "indoor_bench"),
        ("Patio Armchair Set of 2 Outdoor Wicker", "outdoor_chair"),
        ("Set of 4 Dining Chairs Upholstered", "dining_chair"),
    ],
)
def test_true_profiles_still_classify(title, expected_kind):
    profile = classify_listing_profile(title, "")
    assert profile.kind == expected_kind
    assert profile.category_id


def test_workbench_word_boundary_does_not_match_bench_with():
    profile = classify_listing_profile("Heavy Duty Steel Workbench with Pegboard & Drawer", "")
    assert profile.kind != "indoor_bench"
    assert profile.category_id != "262980"


def test_empty_and_none_titles_do_not_crash():
    assert classify_listing_profile("", None, "").kind == "unknown"
    assert classify_listing_profile(None, None, None).kind == "unknown"  # type: ignore[arg-type]


def test_edge_live_dresser_beats_giga_storage_cabinet():
    profile = classify_listing_profile(
        "Walnut 6 Drawer Dresser",
        "",
        "114397",
        source_title="Kids Storage Cabinet for Bedroom, Toddler Wardrobe",
    )
    assert profile.kind != "pantry_cabinet"
    assert profile.category_id != "20487"


def test_edge_kids_work_bench_is_not_seating_bench():
    profile = classify_listing_profile("Kids Work Bench with Pegboard and Stool", "")
    assert profile.kind != "indoor_bench"


def test_edge_velvet_chair_room_list_patio_stays_indoor():
    profile = classify_listing_profile(
        "Upholstered Accent Chair Living Room",
        "",
        "38208",
        source_title="Velvet Lounge Chair for Patio, Balcony, Garden",
    )
    assert profile.kind == "armchair"
    assert profile.category_id == "38208"


COMPOUND_NEGATIVE_CASES = (
    ("7 Piece Dining Table Set with 6 Dining Chairs", "dining_set"),
    ("5 Piece Dining Set with Upholstered Bench and 4 Chairs", "dining_set"),
    ("5 Piece Patio Furniture Set with Ottoman", "patio_furniture_set"),
    ("Patio Conversation Set with 2 Armchairs and Coffee Table", "patio_furniture_set"),
    ("Accent Chair and Ottoman Set", "armchair"),
    ("Swivel Recliner Chair with Ottoman", None),
    ("L-Shaped Sectional Sofa, Reversible Chaise, Storage Ottoman", "sofa"),
    ("Queen Upholstered Platform Bed Frame with Storage Bench", "bed"),
    ("Twin Bunk Bed with Trundle and 2 Nightstands", "bed"),
    ("Entryway Hall Tree with Storage Bench", None),
    ("Shoe Storage Bench", None),
    ("Kitchen Pantry Cabinet with Wine Rack", None),
    ("Dining Table Set for 4 with Upholstered Bench", "dining_set"),
    ("Computer Desk with Storage Bench", "desk"),
)


@pytest.mark.parametrize("title,expected_kind", COMPOUND_NEGATIVE_CASES, ids=[row[0][:40] for row in COMPOUND_NEGATIVE_CASES])
def test_compound_titles_keep_base_behaviour_or_no_remap(title, expected_kind):
    """primary_product_noun early returns must not invent a new profile."""
    from src.utils.product_context_signals import primary_product_noun

    assert primary_product_noun(title) is None
    profile = classify_listing_profile(title, "")
    if expected_kind is None:
        assert profile.kind == "unknown"
        assert not profile.category_id
    else:
        assert profile.kind == expected_kind


def _audit_titles(sku, giga, live, stored, monkeypatch):
    from scripts import audit_fix_active_listings

    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            from src.services.ebay_category_matcher import EbayCategoryMatcher

            class _Oauth:
                def get_application_token(self):
                    return "offline"

            return EbayCategoryMatcher(_Oauth()).canonicalize_category(
                title_context, category_id, category_name, description
            )

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

        def get_category_and_aspects(self, title, aspects, description):
            return stored, None, {}

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())
    opt = {
        "title": live,
        "categoryId": stored,
        "aspects": {
            "Material": ["Wood"],
            "Item Length": ["30 in"],
            "Item Width": ["12 in"],
            "Item Height": ["29 in"],
        },
        "description": "<div>AQUAVERVE KEY FEATURES <li>x</li> SPECIFICATIONS 30 12 29 Ships from California, USA</div>",
    }
    return audit_fix_active_listings.audit_single_product(
        sku, giga, "{}", "{}", json.dumps(opt), "src"
    )


def test_audit_path_real_sku_false_positives_do_not_remap(monkeypatch):
    cases = [
        (
            "W808P475145",
            "Kids 3 Drawer Dresser with Recessed Handle, Baby Nightstand for Bedroom, Storage Cabinet for Toddler",
            "DCW26010-S Sage Wood Nightstand with Anti-Tip Kit 31.5 x 11.8 x 28.9 in",
            "38199",
        ),
        (
            "W3098P470272",
            '65.75" Wide Modern Upholstered Storage Bench With Double Lids, Bench Daybed',
            "65.75 Inch Wide Storage Ottoman Bench Upholstered Napped Fabric Double Sofa",
            "20490",
        ),
        (
            "N817P237961B",
            "Floral Accent Chair Velvet Lounge Arm Chair Living Room Chairs Reading Chair for Balcony",
            "Velvet Accent Armchair with Solid Wood Frame and Metal Legs",
            "38208",
        ),
        (
            "W2105P480697",
            "Leather Armchair Accent Chair Sofa Chairs for Home Office,Bedroom,Dining Room",
            "Modern Brown Faux Leather Accent Chair with Metal Frame, High Back Armchair",
            "38208",
        ),
    ]
    for sku, giga, live, stored in cases:
        issues, fixes = _audit_titles(sku, giga, live, stored, monkeypatch)
        assert not any(issue.get("type") == "category_mismatch" for issue in issues), (sku, issues)
        assert "categoryId" not in fixes, sku


def test_approved_category_is_not_remapped(monkeypatch):
    from scripts import audit_fix_active_listings

    class _Matcher:
        def canonicalize_category(self, *args, **kwargs):
            raise AssertionError("approved listings must not canonicalize")

        def is_category_plausible_for_text(self, *args, **kwargs):
            return False

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())
    monkeypatch.setattr(audit_fix_active_listings, "is_category_approved", lambda opt: True)
    from src.utils.approved_category import CATEGORY_APPROVED_ID_KEY, CATEGORY_APPROVED_KEY

    opt = {
        "title": "Kids Storage Cabinet Nightstand",
        "categoryId": "38199",
        CATEGORY_APPROVED_KEY: True,
        CATEGORY_APPROVED_ID_KEY: "38199",
        "aspects": {
            "Material": ["Wood"],
            "Item Length": ["31 in"],
            "Item Width": ["12 in"],
            "Item Height": ["29 in"],
        },
        "description": "<div>AQUAVERVE KEY FEATURES <li>x</li> SPECIFICATIONS 31 12 29 Ships from California, USA</div>",
    }
    issues, fixes = audit_fix_active_listings.audit_single_product(
        "W808P475145",
        "Kids 3 Drawer Dresser Storage Cabinet for Toddler",
        "{}",
        "{}",
        json.dumps(opt),
        "src",
    )
    assert not any(issue.get("type") == "category_mismatch" for issue in issues)
    assert "categoryId" not in fixes


def _matcher():
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Oauth:
        def get_application_token(self):
            return "offline"

    return EbayCategoryMatcher(_Oauth())


def _proposed_leaves(title, stored, description=""):
    profile = classify_listing_profile(title, description, stored)
    canon, _ = _matcher().canonicalize_category(title, stored, None, description)
    return profile, canon


STAY_ON_LEAF_CASES = (
    (
        "N762P455540D",
        "Outdoor Dining Chairs Set of 2 – Acacia Patio Chairs",
        "79684",
        {"79684"},
        {"54235"},
    ),
    (
        "N762P455581D",
        "Acacia Wood Outdoor Dining Chairs Set of 2",
        "79684",
        {"79684"},
        {"54235"},
    ),
    (
        "N704P417249L",
        "Set of 2 Outdoor Acacia Wood Dining Chairs",
        "79684",
        {"79684"},
        {"54235"},
    ),
    (
        "N768P516661D",
        "Clementine Outdoor Dining Chairs Set of 2 PE Wicker",
        "79684",
        {"79684"},
        {"54235"},
    ),
    (
        "N770P269704G",
        "Acacia Wood Outdoor Dining Chair with Rope Armrests",
        "79684",
        {"79684"},
        {"54235"},
    ),
    (
        "N770P290585B",
        "Outdoor Dining Chairs Set of 2 – Rope & Metal Patio",
        "79684",
        {"79684"},
        {"54235"},
    ),
    (
        "W2105P540786",
        "Velvet Dining Chairs Set of 2 Upholstered Accent Chairs",
        "54235",
        {"54235"},
        {"38208", "79684"},
    ),
    (
        "N770P358870T",
        "Acacia Wood Outdoor Dining Table",
        "107578",
        {"107578", "112590"},
        {"38200", "38204"},
    ),
    (
        "N770P200252C",
        "Outdoor Side Table Cube Patio End Table",
        "112590",
        {"112590", "107578"},
        {"38200", "38204"},
    ),
    (
        "N770P313252G",
        "Acacia Round Outdoor Side Table Patio",
        "112590",
        {"112590", "107578"},
        {"38200", "38204"},
    ),
    (
        "N711P541930A",
        "Outdoor Side Table, Faux Marble",
        "38204",
        {"38204", "112590"},
        {"38200"},
    ),
    (
        "W3098P470275",
        "Upholstered Storage Bench, Bed End Foot",
        "20490",
        {"20490"},
        {"262980", "38208"},
    ),
)


@pytest.mark.parametrize(
    "sku,title,stored,allowed,forbidden",
    STAY_ON_LEAF_CASES,
    ids=[row[0] for row in STAY_ON_LEAF_CASES],
)
def test_replay_wrong_remaps_stay_on_stored_or_outdoor_leaf(sku, title, stored, allowed, forbidden):
    profile, canon = _proposed_leaves(title, stored)
    if profile.category_id:
        assert profile.category_id in allowed, (sku, profile.kind, profile.category_id)
        assert profile.category_id not in forbidden
    assert canon in allowed or canon in {None, stored}
    assert canon not in forbidden


def test_w5368_indoor_side_end_table_still_remaps_off_nightstand():
    title = "Modern Wood Side Table End Table with Storage Shelf for Living Room"
    profile, canon = _proposed_leaves(title, "38199")
    assert profile.kind == "side_table"
    assert profile.category_id == "38200"
    assert canon in {None, "38199", "38200"}
    assert canon != "38199" or profile.category_id == "38200"


def test_w2105_accent_armchair_still_remaps_off_dining_chairs():
    title = "Velvet Accent Chair Armchair, Modern High Back Living Room Chair"
    profile, canon = _proposed_leaves(title, "54235")
    assert profile.kind == "armchair"
    assert profile.category_id == "38208"
    assert canon in {None, "54235", "38208"}
    assert "38208" in {profile.category_id, canon}
