"""PR #33: head-noun-first hold suggestions + r3 low follow-ups."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.category_decision import (
    APPROVED_AUTO_REMAPS,
    gate_publish_category_choice,
    is_approved_auto_remap,
    suggest_category_for_hold,
    suggested_alternative_for,
)
from tests.conftest import load_fixture_category_index

INDEX = load_fixture_category_index()
ROOT = Path(__file__).resolve().parents[1]


def _suggest(title, stored, tree_id="0"):
    return suggest_category_for_hold(title, stored, tree_id=tree_id, index=INDEX)


REAL_SKU_CASES = [
    (
        1,
        "W2311P420441",
        "38200",
        "Mid-Back Lifting Office Chair Ergonomic Design with Memory Foam Black Corduroy",
        "54235",
        {"38204"},
        "med",
    ),
    (
        2,
        "W2311P420443",
        "38200",
        "Ergonomic Mid-Back Office Chair with Gold Base, Memory Foam Cushion, 25.5×23.5×42 in, Gray",
        "54235",
        {"38204"},
        "med",
    ),
    (
        3,
        "W2899P372743",
        "38200",
        "34 Inch Wide Sideboard Cabinet with Glass Doors Adjustable Shelf Dark Oak MDF",
        "183322",
        {"38204"},
        "high",
    ),
    (
        4,
        "W2899P372741",
        "68240",
        "49.33 Inch Sideboard Cabinet with 3 Mirrored Doors Adjustable Shelves Brown MDF",
        "183322",
        {"103430"},
        "high",
    ),
    (
        5,
        "W3916P426311",
        "177816",
        'Metal Dining Chairs Set of 2 – Modern Fabric Upholstered, 33"H x 19"W x 17.5"D, Beige',
        "54235",
        {"107578"},
        "high",
    ),
    (
        6,
        "W1143141069",
        "177816",
        "French Style Dining Chair Set of 2 with Rattan Back and Linen Fabric Gray",
        "54235",
        {"107578"},
        "high",
    ),
    (
        7,
        "N704P383163A",
        "118218",
        "Outdoor Counter Height Bar Stools Set of 2 with Backrest Arm Webbing Seat Beige Cushions A",
        "103431",
        {"54235"},
        "med",
    ),
    (
        8,
        "W3258P448840",
        "20466",
        "37 Inch Middle Size Left Drawer Desktop Vanity Table + Cushioned Stool Large Sliding Mirro",
        "32878",
        {"114397", "88057", "103431"},
        "high",
    ),
    (
        9,
        "W331P372820",
        "177000",
        '6-Tier Slim Rolling Kitchen Cart with Handle & Wheels 11.8" Deep Black',
        "115753",
        {"260157"},
        "high",
    ),
    (
        10,
        "W46558450",
        "",
        "10 in 1 Multi-Functional Trimming Tool, 52CC 2-Cycle Garden Tool System with Gas Pole Saw,",
        "71277",
        {"38208"},
        "med",
    ),
    (
        11,
        "W46561892",
        "",
        "Backpack 4 in 1 Multi-Functional Trimming Tool, 38CC 4-stroke Garden Tool System with Gas ",
        "71277",
        {"38208"},
        "med",
    ),
    (
        12,
        "W465P185060",
        "",
        "55.7cc Mosquito fogger Mister Duster Mosquito Sprayer Backpack Sprayer Cold Fogger,Gasolin",
        "181037",
        {"38208"},
        "med",
    ),
    (
        13,
        "W465P456848",
        "",
        '12" Swing Benchtop Drill Press with Laser & Digital Meter, Variable Speed, 0-45 Degree Til',
        "71296",
        {"88057", "262980"},
        "high",
    ),
    (
        14,
        "W46536791",
        "",
        "Stainless Steel Fruit and Wine Press 3.69gallon/14L",
        "260139",
        {"38208", "71296"},
        "med",
    ),
    (
        15,
        "W1435P312263",
        "42428",
        "23.6 Inch Round Metal Framed Medicine Cabinet with Mirror and Adjustable Shelves Black",
        "176991",
        {"20580"},
        "high",
    ),
    (
        16,
        "N707P233747B",
        "",
        '52.8"W Wicker Outdoor Grill Cart with Extended Plastic Wood Table Top, Rolling Kitchen Isl',
        "260931",
        {"20487", "38204"},
        "med",
    ),
    (
        17,
        "W3190P328545",
        "38200",
        "Outdoor Grill Cart for Two Big Green Egg 102 Inch Metal Table with Wheels",
        "260931",
        {"38204"},
        "med",
    ),
    (
        18,
        "W3190P305785",
        "177000",
        "Outdoor Grill Table for Big Green Egg Large 22 Inch, Metal Cart with Folding Panels Black",
        "112590",
        {"260157"},
        "med",
    ),
    (
        19,
        "N707S000010B",
        "",
        "K&K 82.7 inch Kitchen Island with Extendable Dining Table for 4-6 Person,Faux Mable Kitche",
        "260157",
        {"38204"},
        "med",
    ),
    (
        20,
        "TWSET2025-3N",
        "",
        "6-Piece Small Farmhouse Solid Wood Kitchen Nook Dining Room Breakfast Nook Table Set for 6",
        "107578",
        {"54235"},
        "high",
    ),
    (
        21,
        "N711P230809B",
        "38205",
        "59.1 in Entryway Console Table with Pine Veneer Top & Solid Pine Legs - Black",
        "38204",
        set(),
        "high",
    ),
    (
        22,
        "N775P528961G",
        "175764",
        "Freestanding Fireplace Screen 50 Inch Wide Gold Iron Mesh Spark Barrier",
        "38221",
        {"31601"},
        "high",
    ),
    (
        23,
        "W1247118742",
        "25306",
        "3-Drawer Locking Metal Filing Cabinet for Office Home Garage Storage 23x17x40",
        "3299",
        set(),
        "med",
    ),
    (
        24,
        "W2367P551869",
        "22513",
        "Ergonomic Gaming Chair with Massage Lumbar & Footrest 49.2 in White",
        "54235",
        {"38208"},
        "med",
    ),
    (
        25,
        "MS306993AAL",
        "79693",
        "13ft Geometric Dome Climber Play Center Kids Jungle Gym Tower with Hammock Outdo",
        "16515",
        {"79693"},
        "med",
    ),
    (
        26,
        "N767P290454T",
        "38200",
        'Modern Minimalist Magnesium Oxide Side Table 14.5" Square Beige Natural Finish',
        "38204",
        set(),
        "high",
    ),
    (
        27,
        "N707S000028B",
        "177000",
        "72 Inch Farmhouse Kitchen Island with Fridge Space Power Outlet Rolling Bar",
        "260157",
        {"115753"},
        "high",
    ),
    (
        28,
        "N707S186617B",
        "177000",
        "Drop Leaf Kitchen Island Cart Rolling Storage Cabinet with Power Outlet Drawers Rack Black",
        "260157",
        {"115753"},
        "high",
    ),
    (
        29,
        "N721S000075B-1",
        "261263",
        "Farmhouse Hall Tree with 8 Hooks and Bench 59x15.7x76.7 in Black Particle Board+",
        "22656",
        {"262980"},
        "high",
    ),
    (
        30,
        "N752S0010618B",
        "177816",
        "5-Piece Round Dining Table Set for 4 People, 42.36 Inch Wood Table + Upholstered Chairs, B",
        "107578",
        {"54235"},
        "high",
    ),
    (
        31,
        "W1668P147059",
        "38205",
        'Long Bar Table Set with 3 PU Upholstered Stools 63"L x 15.7"W x 37.5"H Rustic Brown Black',
        "107578",
        {"103431"},
        "med",
    ),
    (
        32,
        "N770P281696C",
        "",
        "Outdoor Chaise Lounge Chair with 6-Position Adjustable Backrest,Acacia solid wood,Copper T",
        "79684",
        {"54235"},
        "high",
    ),
    (
        33,
        "N770P264304T",
        "",
        "Outdoor Acacia Wood Club Wooden Armchair with Cushions (Set of 2), Conversation Set,Teak F",
        "79682",
        {"38208"},
        "high",
    ),
]


@pytest.mark.parametrize(
    "row_id,sku,stored,title,expected,must_not,conf",
    REAL_SKU_CASES,
    ids=[f"{row[0]}-{row[1]}" for row in REAL_SKU_CASES],
)
def test_real_sku_hold_suggestion_table(row_id, sku, stored, title, expected, must_not, conf):
    got = _suggest(title, stored)
    assert got is not None, (sku, title)
    assert got.category_id == expected, (sku, got)
    assert got.confidence == conf, (sku, got)
    assert got.category_id not in must_not
    wrapper = suggested_alternative_for(stored, "", title, tree_id="0", index=INDEX)
    assert wrapper == expected


def test_row34_human_dog_bed_is_low_or_none():
    title = "Human Dog Bed Convertible Sofa Lounge 78.7 Inch 2-Seat Teddy Fabric Grey"
    got = _suggest(title, "20751")
    assert got is None or (got.category_id == "38208" and got.confidence == "low")
    if got is not None:
        assert not (got.category_id == "20744" and got.confidence == "high")


def test_row35_only_table_top_is_low_or_none():
    title = "ONLY TABLE TOP.DT-OX-16090-S-10MJD-TOP"
    got = _suggest(title, "")
    assert got is None or (got.category_id == "38204" and got.confidence == "low")
    if got is not None:
        assert got.confidence != "high"


def test_row36_grill_cart_in_barbecue_is_not_held():
    title = "80.5 Inch Outdoor Kitchen Island Grill Cart Table with Storage"
    gated, action, err = gate_publish_category_choice(
        "20725", "20725", title, "READY", index=INDEX
    )
    assert (gated, action, err) == ("20725", "keep", "")


SYNTHETIC_CASES = [
    (
        "S1",
        "SYNTHETIC Stable Storage Rack with Tablet Holder 47 Inch",
        "38200",
        None,
        {"38204"},
        None,
        "0",
    ),
    (
        "S2",
        "SYNTHETIC Desktop Organizer Benchtop Shelf",
        "",
        None,
        {"88057", "262980"},
        None,
        "0",
    ),
    (
        "S3",
        "SYNTHETIC 47 Inch Chairs Cushion Cover 2 Pack",
        "54235",
        None,
        {"107578"},
        None,
        "0",
    ),
    (
        "S4",
        "SYNTHETIC Garlic Press Stainless Steel",
        "",
        None,
        {"71296", "260139"},
        None,
        "0",
    ),
    (
        "S5",
        "SYNTHETIC Entryway Bench fits under Hall Tree",
        "262980",
        "262980",
        {"22656"},
        "high",
        "0",
    ),
    (
        "S6",
        "SYNTHETIC Kitchen Cart and Island Combo",
        "177000",
        "115753",
        set(),
        None,
        "0",
    ),
    (
        "S7",
        "SYNTHETIC Office Chair Mat 36x48 in",
        "38200",
        None,
        {"54235"},
        None,
        "0",
    ),
    (
        "S8",
        "SYNTHETIC Rolling Tool Chest",
        "",
        None,
        {"20487"},
        None,
        "100",
    ),
    (
        "S9",
        'SYNTHETIC 1/2" Hex Drive Storage Rack',
        "",
        None,
        {"38204", "54235"},
        None,
        "0",
    ),
    (
        "S10",
        "SYNTHETIC OFFICE CHAIR 60CM SEAT WIDTH",
        "38200",
        "54235",
        {"38204"},
        "med",
        "0",
    ),
    (
        "S11",
        "SYNTHETIC Storage Rack 20-22 Inch Black",
        "38200",
        None,
        {"38204"},
        None,
        "0",
    ),
    (
        "S12",
        "SYNTHETIC 2-in-1 Office Chair and Footrest Combo",
        "38200",
        "54235",
        {"38204"},
        "med",
        "0",
    ),
    (
        "S13",
        "SYNTHETIC Bar Stools for Kitchen Island",
        "",
        "103431",
        {"260157", "54235"},
        "med",
        "0",
    ),
    (
        "S14",
        "SYNTHETIC OUTDOOR CHAISE LOUNGE CHAIR WITH CUSHION",
        "",
        "79684",
        {"54235"},
        "high",
        "0",
    ),
    (
        "S15",
        "SYNTHETIC Dining Chairs Set of 2 — Walnut Frame",
        "177816",
        "54235",
        {"107578"},
        "high",
        "0",
    ),
    (
        "S16",
        'SYNTHETIC Makeup Vanity with Stool 22" Wide',
        "",
        "32878",
        {"88057", "114397"},
        "high",
        "0",
    ),
    (
        "S17",
        "SYNTHETIC Hall Tree for Bench Storage",
        "261263",
        "22656",
        {"262980"},
        "high",
        "0",
    ),
    (
        "S18",
        "SYNTHETIC Kitchen Island 47cm Drop Leaf",
        "177000",
        "260157",
        {"115753"},
        "high",
        "0",
    ),
]


@pytest.mark.parametrize(
    "row_id,title,stored,expected,must_not,conf,tree_id",
    SYNTHETIC_CASES,
    ids=[row[0] for row in SYNTHETIC_CASES],
)
def test_synthetic_hold_suggestion_table(row_id, title, stored, expected, must_not, conf, tree_id):
    got = _suggest(title, stored, tree_id=tree_id)
    if expected is None:
        assert got is None or got.category_id not in must_not
        if row_id == "S3" and got is not None:
            assert not (got.category_id == "54235" and got.confidence == "high")
        if row_id == "S6":
            assert got is not None
            assert got.category_id == "115753"
            assert not (got.category_id == "260157" and got.confidence == "high")
        if got is not None:
            assert got.category_id not in must_not
        return
    assert got is not None, (row_id, title)
    assert got.category_id == expected, (row_id, got)
    if conf:
        assert got.confidence == conf, (row_id, got)
    assert got.category_id not in must_not


def test_s3_accessory_never_high_chair_or_dining_set():
    got = _suggest("SYNTHETIC 47 Inch Chairs Cushion Cover 2 Pack", "54235")
    assert got is None or got.confidence != "high"
    if got is not None:
        assert got.category_id != "107578"


def test_s6_kitchen_cart_combo_is_not_island_at_high():
    got = _suggest("SYNTHETIC Kitchen Cart and Island Combo", "177000")
    assert got is not None
    assert got.category_id == "115753"
    assert not (got.category_id == "260157" and got.confidence == "high")


def test_approved_auto_remaps_stays_empty():
    assert APPROVED_AUTO_REMAPS == frozenset()
    assert is_approved_auto_remap("125085", "20487", "publish_candidate") is False


def test_suggestion_wrapper_never_writes():
    from src.utils.category_decision import decision_category_id, resolve_category_decision

    decision = resolve_category_decision(
        "Mid-Back Lifting Office Chair Ergonomic Design with Memory Foam Black Corduroy",
        "",
        "",
        "38200",
        "",
        index=INDEX,
    )
    assert decision_category_id(decision, "38200", index=INDEX) is None


def test_pr29_keeps_unchanged():
    cases = [
        ("W1321S00027", "88057", "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak"),
        ("W1321S00028", "88057", "L-Shaped Computer Desk with Storage Shelves 66 Inch Walnut"),
        ("W1580S00493", "88057", "L-Shaped Desk with Drawers Home Office Workstation"),
        ("gp:W2530P418996", "38208", "Indoor Wooden Rocking Chair with Cushion Living Room"),
        ("N767P369236T", "79682", "Outdoor Chaise Lounge Chair Adjustable Backrest Acacia Wood"),
    ]
    for sku, stored, title in cases:
        gated, action, err = gate_publish_category_choice(
            stored, stored, title, "READY", index=INDEX
        )
        assert (gated, action, err) == (stored, "keep", ""), (sku, action, err)


def test_m2_grill_table_rows_still_held():
    cases = [
        (
            "W3190S00002",
            "125085",
            "5-Shelf Glass Display Cabinet 71 Inch Tall with Tempered Glass Shelves",
        ),
        (
            "W3190P305785",
            "177000",
            "Outdoor Grill Table for Big Green Egg Large 22 Inch, Metal Cart with Folding Panels Black",
        ),
        (
            "W3190P305792",
            "177000",
            "Outdoor Grill Table for Big Green Egg Large 20 21 22 Inch Kamado Joe 67in",
        ),
    ]
    for sku, stored, title in cases:
        gated, action, err = gate_publish_category_choice(
            stored, stored, title, "READY", index=INDEX
        )
        assert action == "hold_implausible", (sku, action, err)
        assert gated is None
        if sku == "W3190P305785":
            assert "112590" in err
            assert "260157" not in err


def test_tree_unavailable_hold_is_action_only():
    from batch_publish import is_tree_unavailable_hold

    assert is_tree_unavailable_hold({"action": "hold_tree_unavailable"}) is True
    assert (
        is_tree_unavailable_hold(
            {"action": "hold_empty", "message": "SYNTHETIC tree house unavailable"}
        )
        is False
    )
    assert is_tree_unavailable_hold({"message": "tree 0 unavailable"}) is False
    assert is_tree_unavailable_hold({"status": "error"}) is False


def test_r3f_batch_main_stops_after_first_tree_unavailable(monkeypatch, tmp_path):
    import batch_publish as bp

    calls = []
    products = [
        {
            "sku": f"SYNTHETIC-R3F-{suffix}",
            "title": "Patio Chair",
            "suggested_price": 10,
            "optimization": {"title": "Patio Chair", "categoryId": "79682"},
        }
        for suffix in ("A", "B", "C")
    ]

    def _publish(product, dry_run=False):
        calls.append(product["sku"])
        return {
            "status": "error",
            "message": "category tree unavailable; refresh failed; no publish",
            "action": "hold_tree_unavailable",
        }

    monkeypatch.setattr(bp, "publish_single_product", _publish)
    monkeypatch.setattr(bp, "get_ready_products", lambda *a, **k: list(products))
    monkeypatch.setattr(bp.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(bp, "LOG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["batch_publish.py"])
    bp.main()
    assert calls == ["SYNTHETIC-R3F-A"]


def test_gate_tests_leave_no_repo_root_dbs():
    import os

    import src.services.ebay_auth as ebay_auth
    import src.services.ebay_category_validator as validator_mod

    override = Path(os.environ["DAJIAN_COLLECTION_DB"]).resolve()
    assert override != (ROOT / "ebay_collection.db").resolve()
    assert Path(ebay_auth._TOKEN_DB_PATH).resolve() != (ROOT / "ebay_tokens.db").resolve()
    assert Path(validator_mod.default_cache_db_path()).resolve() != (ROOT / "ebay_collection.db").resolve()


def _run_approved_retry_branch(monkeypatch, sku, title, stored, target, attrs, ai_height="99 in"):
    import asyncio

    import server as srv
    from fastapi import BackgroundTasks
    import src.utils.category_decision as cd

    monkeypatch.setattr(
        cd,
        "APPROVED_AUTO_REMAPS",
        frozenset({(stored, target)}),
    )
    stored_opt = {
        "categoryId": stored,
        "categoryName": "Category",
        "title": title,
        "description": "<div>" + ("A complete product description. " * 30) + "</div>",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Item Height": [ai_height],
        },
    }

    class _Product:
        status = "READY"
        listing_id = None
        optimization = dict(stored_opt)
        cost_breakdown = {"base": 10}
        suggested_price = 199.0
        price = 199.0
        description = stored_opt["description"]
        images = ["https://example.com/a.jpg"]
        videos = []
        specs = {}
        logs = []

    product = _Product()
    product.sku = sku
    product.title = title
    product.attributes = attrs

    class _DB:
        def query(self, model):
            class _Q:
                def filter_by(self, **kw):
                    class _R:
                        def first(self):
                            return product

                    return _R()

            return _Q()

        def commit(self):
            return None

    class _Oauth:
        def is_authorized(self):
            return True

        def get_application_token(self):
            return "t"

        def get_valid_token(self):
            return "t"

    published = {"attempts": 0}

    class _Client:
        oauth = _Oauth()
        base_url = "https://api.ebay.com"

        def upload_images_to_eps(self, images, max_images=24):
            return list(images)

        def create_or_replace_inventory_item(self, **kw):
            published["attempts"] += 1
            if published["attempts"] == 1:
                raise Exception("invalid category")
            published["aspects"] = kw["product"]["aspects"]
            return True

        def create_or_replace_product_compatibility(self, *a, **k):
            return True

        def delete_product_compatibility(self, *a, **k):
            return True

        def create_offer(self, **kw):
            published["category_id"] = kw["category_id"]
            return {"offerId": "OFFER1"}

        def publish_offer(self, offer_id):
            return {"listingId": "123"}

        def get_offers_by_sku(self, sku):
            return [{"categoryId": target, "listing": {"listingId": "123"}}]

    monkeypatch.setattr("src.clients.real_ebay_client.create_real_ebay_client", lambda env: _Client())

    class _Matcher:
        def canonicalize_category(self, *a, **k):
            return target, "Target"

        def get_category_and_aspects(self, *a, **k):
            return target, "Target", dict(stored_opt["aspects"])

        def is_category_plausible_for_text(self, *a, **k):
            return True

        def _get_category_aspects(self, cid):
            return ([], [])

        def _complete_aspects(self, existing, *a, **k):
            return dict(existing)

    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", lambda oauth: _Matcher())
    monkeypatch.setattr(
        "src.services.ebay_publisher.apply_category_publish_gate",
        lambda **kw: (target, "Target", dict(stored_opt["aspects"])),
    )
    monkeypatch.setattr(srv, "_get_publish_blockers", lambda *a, **k: [])
    monkeypatch.setattr(srv, "complete_publish_aspects", lambda aspects, **k: aspects)
    monkeypatch.setattr(srv, "flag_modified", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "src.utils.approved_category.evaluate_approved_category",
        lambda *a, **k: type("D", (), {"approved": False, "preserve": False, "error": ""})(),
    )
    monkeypatch.setattr("src.utils.ebay_quantity.resolve_publish_quantity", lambda *a, **k: 1)

    result = asyncio.run(srv.publish_product(sku, BackgroundTasks(), _DB()))
    return result, published, product


def test_r3_approved_retry_reapplies_source_dims_w3190(monkeypatch):
    result, published, product = _run_approved_retry_branch(
        monkeypatch,
        "W3190S00002",
        "5-Shelf Glass Display Cabinet 71 Inch Tall with Tempered Glass Shelves",
        "125085",
        "20487",
        {"Length": "23.6", "Width": "15.7", "Height": "72.0", "Product Weight (lbs.)": "40"},
    )
    assert result["status"] == "success", result
    assert published["attempts"] >= 2
    assert published["aspects"]["Item Height"] == ["72.0 in"]
    assert product.optimization["aspects"]["Item Height"] == ["72.0 in"]
    assert APPROVED_AUTO_REMAPS == frozenset()


def test_r3_approved_retry_reapplies_source_dims_w3916(monkeypatch):
    result, published, product = _run_approved_retry_branch(
        monkeypatch,
        "W3916P426311",
        'Metal Dining Chairs Set of 2 – Modern Fabric Upholstered, 33"H x 19"W x 17.5"D, Beige',
        "125085",
        "20487",
        {
            "Length": "19.0",
            "Width": "17.5",
            "Height": "33.1",
            "Product Weight (lbs.)": "12.0",
        },
    )
    assert result["status"] == "success", result
    assert published["aspects"]["Item Height"] == ["33.1 in"]
    assert published["aspects"]["Item Length"] == ["19.0 in"]
    assert published["aspects"]["Item Width"] == ["17.5 in"]
    assert published["aspects"]["Item Weight"] == ["12.0 lbs"]
    assert product.optimization["aspects"]["Item Height"] == ["33.1 in"]
    assert APPROVED_AUTO_REMAPS == frozenset()
