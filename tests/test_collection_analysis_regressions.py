import ast
from pathlib import Path

import pytest

import qwen_optimizer
from qwen_optimizer import QwenOptimizer
from scripts.audit_fix_ready_drafts import choose_ready_category
from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_category_matcher import EbayCategoryMatcher
from src.utils.dimension_helpers import extract_all_dimensions
from src.utils.dimension_helpers import find_dimension
from src.utils.dimension_helpers import replace_description_measurements
from src.utils.dimension_helpers import replace_description_weight_placeholder_with_package_weight
from src.utils.publish_aspect_completion import infer_set_includes
from src.utils.publish_autofix import sanitize_placeholder_aspects, try_fix_publish_error
from src.utils.publish_validation import measurement_validation_errors
from scripts.audit_fix_ready_drafts import (
    build_dimension_image_note,
    infer_known_layout_measurements,
)


ROOT = Path(__file__).resolve().parents[1]


def test_qwen_timeout_wrapper_falls_back_when_subprocess_exits(monkeypatch):
    class FakeQueue:
        def get_nowait(self):
            raise qwen_optimizer.queue_module.Empty()

    class FakeProcess:
        def __init__(self):
            self.exitcode = 1

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    class FakeContext:
        def Queue(self, maxsize=1):
            return FakeQueue()

        def Process(self, target=None, args=None, daemon=None):
            return FakeProcess()

    class FakeOptimizer:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def optimize_product_full(self, **kwargs):
            return {
                "title": "Fallback Direct Run",
                "description": "ok",
                "aspects": {"Brand": ["AquaVerve"]},
                "kwargs_seen": kwargs,
            }

    monkeypatch.setattr(qwen_optimizer.multiprocessing, "get_context", lambda mode: FakeContext())
    monkeypatch.setattr(qwen_optimizer, "QwenOptimizer", FakeOptimizer)

    result = qwen_optimizer.optimize_product_full_with_timeout(
        api_key="dummy",
        original_title="Test Product",
        original_description="desc",
        attributes={},
        specs={},
        images=[],
    )

    assert result["title"] == "Fallback Direct Run"
    assert result["kwargs_seen"]["original_title"] == "Test Product"


def test_qwen_optimizer_defaults_to_qwen_plus_latest(monkeypatch):
    created = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(qwen_optimizer, "OpenAI", FakeOpenAI)

    optimizer = QwenOptimizer(api_key="dashscope-test-key")

    assert optimizer.model == "qwen-plus-latest"
    assert created["api_key"] == "dashscope-test-key"
    assert created["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def test_server_analysis_defines_dajian_costs_before_use():
    func = _function_node(ROOT / "server.py", "analyze_product_task")
    stores = []
    loads = []
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == "dajian_costs":
            if isinstance(node.ctx, ast.Store):
                stores.append(node.lineno)
            elif isinstance(node.ctx, ast.Load):
                loads.append(node.lineno)

    assert stores, "analyze_product_task must calculate dajian_costs"
    assert loads, "test should observe dajian_costs usage"
    assert min(stores) < min(loads)


def test_qwen_dimension_extraction_ignores_package_dimensions_for_item_specifics():
    optimizer = object.__new__(QwenOptimizer)

    dimensions = optimizer.extract_dimensions(
        attributes={},
        specs={
            "Package Length (in.)": "80",
            "Package Width (in.)": "40",
            "Package Height (in.)": "20",
            "Package Weight (lbs.)": "160",
        },
        description="",
    )

    assert dimensions["length"] is None
    assert dimensions["width"] is None
    assert dimensions["height"] is None
    assert dimensions["weight"] is None


def test_qwen_dimension_extraction_uses_product_dimensions_from_specs():
    optimizer = object.__new__(QwenOptimizer)

    dimensions = optimizer.extract_dimensions(
        attributes={},
        specs={
            "Assembled Length (in.)": "72",
            "Assembled Width (in.)": "31.5",
            "Assembled Height (in.)": "28",
            "Product Weight (lbs.)": "85",
        },
        description="",
    )

    assert dimensions["length"] == "72"
    assert dimensions["width"] == "31.5"
    assert dimensions["height"] == "28"
    assert dimensions["weight"] == "85 lbs"


def test_error_draft_known_layout_measurements_from_dimension_images():
    assert infer_known_layout_measurements(
        "W714S01794",
        "Modern U-Shaped Modular Sectional Sofa for Living Room Corduroy Fabric 4 Seat",
    ) == {
        "Assembled Length (in.)": "151.9",
        "Assembled Width (in.)": "69.7",
        "Assembled Height (in.)": "27.1",
    }
    assert infer_known_layout_measurements(
        "W714S01587",
        "Modular Sectional Sofafor Living Room, 6 - Black + Corduroy + 6 Seat",
    ) == {
        "Assembled Length (in.)": "151.1",
        "Assembled Width (in.)": "116.1",
        "Assembled Height (in.)": "27.1",
    }
    assert infer_known_layout_measurements(
        "W714S01590",
        "Modular Sectional Sofafor Living Room, 6 - Camel + Corduroy + 4 Seat",
    ) == {
        "Assembled Length (in.)": "151.1",
        "Assembled Width (in.)": "116.1",
        "Assembled Height (in.)": "27.1",
    }
    assert infer_known_layout_measurements(
        "W714S00449",
        "Comfy Deep Single Seat Sofa Upholstered Reading Armchair Living Room Chair",
    ) == {
        "Assembled Length (in.)": "40.9",
        "Assembled Width (in.)": "34.2",
        "Assembled Height (in.)": "33.0",
    }

    note = build_dimension_image_note(
        "W714S01794",
        "Modern U-Shaped Modular Sectional Sofa for Living Room Corduroy Fabric 4 Seat",
        {},
    )
    assert "151.9 in L x 69.7 in W x 27.1 in H" in note
    assert "complex modular shape" in note


def test_golf_products_are_not_pet_furniture_category():
    matcher = object.__new__(EbayCategoryMatcher)

    assert not matcher.is_category_plausible_for_text(
        "Golf Bag Organizer for Garage Premium Wooden Golf Clubs Storage Rack",
        "20740",
        "Furniture & Scratchers",
    )
    assert not matcher.is_category_plausible_for_text(
        "Golf Practice Net Cage 10 x 10 x 10FT Metal Frame Hitting Net Kit",
        "20740",
        "Furniture & Scratchers",
    )
    assert matcher._fallback_category(
        "Golf Bag Organizer for Garage Premium Wooden Golf Clubs Storage Rack"
    )[0] == "30109"
    assert matcher._fallback_category(
        "Golf Practice Net Cage 10 x 10 x 10FT Metal Frame Hitting Net Kit"
    )[0] == "50876"


def test_outdoor_gazebo_and_recliner_categories_are_not_cross_matched():
    matcher = object.__new__(EbayCategoryMatcher)
    gazebo_title = "5x3 Wooden Gazebo for Patio Garden Outdoor Canopy with Waterproof Top and Tabletop"
    recliner_title = "Swivel Chenille Recliner with Ottoman Chair Manual Reclining Footrest Bedroom Living Room"

    assert matcher._fallback_category(gazebo_title)[0] == "180995"
    assert not matcher.is_category_plausible_for_text(gazebo_title, "88057", "Desks")
    assert matcher.is_category_plausible_for_text(gazebo_title, "180995", "Gazebos")

    assert matcher._fallback_category(recliner_title)[0] == "54235"
    assert not matcher.is_category_plausible_for_text(recliner_title, "175758", "Bed Frames")
    assert not matcher.is_category_plausible_for_text(recliner_title, "88057", "Desks")
    assert matcher.is_category_plausible_for_text(recliner_title, "54235", "Chairs")


def test_outdoor_fountains_do_not_publish_as_pet_furniture_or_plant_stands():
    matcher = object.__new__(EbayCategoryMatcher)
    fountain_titles = [
        'LED Concrete Column Fountain 25.59 in Tall Minimalist Outdoor Water Feature',
        '5-Tiered Concrete Tall Slim Fountain 23.75 in H LED-Lit Column',
        '4-Tiered Concrete Rock Waterfall Fountain with LED Lights Outdoor Garden Decor',
        '2-Tier Outdoor Water Fountain with LED Lights Classic Pedestal Cascading Water Feature',
    ]

    for title in fountain_titles:
        assert matcher._fallback_category(title)[0] == "20507"
        assert matcher.canonicalize_category(title, "20740", "Furniture & Scratchers") == (
            "20507",
            "Outdoor Fountains",
        )
        assert matcher.canonicalize_category(title, "29514", "Plant Stands") == (
            "20507",
            "Outdoor Fountains",
        )
        assert matcher.is_category_plausible_for_text(title, "20507", "Outdoor Fountains")


def test_garden_stools_do_not_publish_as_pet_furniture():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Elephant Garden Stool Copper Patina Modern Decorative Outdoor Accent"

    assert matcher._fallback_category(title)[0] == "79682"
    assert matcher.is_category_plausible_for_text(title, "79682", "Patio Chairs")
    assert not matcher.is_category_plausible_for_text(title, "29511", "Ornaments & Statues")
    assert not matcher.is_category_plausible_for_text(title, "20740", "Furniture & Scratchers")


def test_frog_garden_stool_with_statue_context_publishes_as_garden_decor():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Garden Frog Sitting on Rock Statue Decor - 13 Inch High Lightweight Concrete Frog Garden Stool"

    assert matcher._fallback_category(title)[0] == "29511"
    assert matcher.canonicalize_category(title, "79682", "Patio Chairs") == (
        "29511",
        "Ornaments & Statues",
    )
    assert matcher.is_category_plausible_for_text(title, "29511", "Ornaments & Statues")


def test_ready_draft_category_regressions_from_may_batch():
    matcher = object.__new__(EbayCategoryMatcher)

    sofa_title = "Modern Style L-Shaped Sectional Sofa with USB Ports Bluetooth Speaker 102 in Black"
    assert matcher._fallback_category(sofa_title)[0] == "38208"
    assert matcher.canonicalize_category(sofa_title, "63862", "Coats, Jackets & Vests") == (
        "38208",
        "Sofas, Armchairs & Couches",
    )

    ottoman_title = "50.8 Inch Multi-Functional Storage Ottoman Bench with Smart Lift Top Linen Seat Walnut Closet"
    assert matcher._fallback_category(ottoman_title)[0] == "20490"
    assert matcher.canonicalize_category(ottoman_title, "103430", "Armoires & Wardrobes") == (
        "20490",
        "Ottomans, Footstools & Poufs",
    )

    coop_title = "Large Wooden Chicken Coop Outdoor Hen House with Run Nesting Box Poultry Cage Rabbit Hutch"
    assert matcher._fallback_category(coop_title)[0] == "63108"
    assert matcher.is_category_plausible_for_text(coop_title, "63108", "Cages, Hutches & Enclosure")

    kids_title = "Kids Activity Table and Chair Set for Toddlers Ages 2-6 Wooden MDF Yellow White"
    assert matcher._fallback_category(kids_title)[0] == "66743"
    assert matcher.canonicalize_category(kids_title, "66756", "Kids Tables") == (
        "66743",
        "Play Table & Chair Sets",
    )

    wine_title = "Fluted Wine Cabinet with Glass Display and Stemware Rack 71 Inch Modern Bar Cabinet"
    assert matcher._fallback_category(wine_title)[0] == "20689"
    assert matcher.canonicalize_category(wine_title, "45331", "Wine Racks") == (
        "20689",
        "Wine Racks & Bottle Holders",
    )

    patio_table_title = "Outdoor Steel Slat Patio Dining Table Square Backyard Bistro Table"
    assert matcher._fallback_category(patio_table_title)[0] == "112590"
    assert matcher.canonicalize_category(patio_table_title, "38204", "Dining Tables") == (
        "112590",
        "Patio & Garden Tables",
    )
    assert matcher.is_category_plausible_for_text(patio_table_title, "112590", "Patio & Garden Tables")

    bunk_with_sofa_title = "Twin Over Twin Bunk Bed Frame with Storage Drawers Lower Bed Can Convert Into Sofa"
    assert matcher._fallback_category(bunk_with_sofa_title)[0] == "175758"
    assert matcher.canonicalize_category(bunk_with_sofa_title, "175754", "Bunk Beds") == (
        "175758",
        "Beds & Bed Frames",
    )
    assert matcher.is_category_plausible_for_text(bunk_with_sofa_title, "175758", "Beds & Bed Frames")


def test_ready_draft_category_regressions_from_may_18_batch():
    matcher = object.__new__(EbayCategoryMatcher)

    swing_bed_title = "Outdoor Classic Solid Wood Twin Size Swing Bed Garden Porch Rattan Swing Frame with Long Ropes for Poolside Backyard"
    assert matcher.canonicalize_category(swing_bed_title, "20740", "Furniture & Scratchers") == (
        "79694",
        "Porch Swings",
    )
    assert matcher.is_category_plausible_for_text(swing_bed_title, "79694", "Porch Swings")

    desk_title = "L Shaped Desk Computer Desk with Drawers Bookshelf LED Light Modern Corner Desk Home Office Desk"
    assert matcher.canonicalize_category(desk_title, "3199", "Bookcases") == (
        "88057",
        "Desks & Tables",
    )
    assert matcher.is_category_plausible_for_text(desk_title, "88057", "Desks & Tables")

    club_chair_title = "Set of 2 Outdoor Acacia Wood Club Chairs with Modern Square Arms Patio Conversation Set"
    assert matcher.canonicalize_category(club_chair_title, "139849", "Patio & Garden Furniture Sets") == (
        "79684",
        "Outdoor Chairs",
    )
    assert matcher.is_category_plausible_for_text(club_chair_title, "79684", "Outdoor Chairs")

    outdoor_daybed_title = "Outdoor Acacia Wood Round Daybed Patio Lounger with Curved Slatted Backrest Cushion and 4 Pillows"
    assert matcher.canonicalize_category(outdoor_daybed_title, "175758", "Beds & Bedframes") == (
        "138996",
        "Outdoor Daybeds",
    )
    assert matcher.is_category_plausible_for_text(outdoor_daybed_title, "138996", "Outdoor Daybeds")

    dining_set_title = "5-Piece Outdoor Acacia Wood Round Dining Set Solid Wood Patio Table with 4 Barrel Chairs"
    assert matcher.canonicalize_category(dining_set_title, "139849", "Patio & Garden Furniture Sets") == (
        "107578",
        "Dining Sets",
    )
    assert matcher.is_category_plausible_for_text(dining_set_title, "107578", "Dining Sets")

    putting_green_title = "12x5 FT Golf Putting Green Golf Training Mat with Ball Return"
    assert matcher.canonicalize_category(putting_green_title, "20740", "Furniture & Scratchers") == (
        "36234",
        "Putting Greens & Aids",
    )
    assert matcher.is_category_plausible_for_text(putting_green_title, "36234", "Putting Greens & Aids")

    hitting_mat_title = "5x4 FT Golf Hitting Mat with Cross Stance Guide and 7 Tee Positions"
    assert matcher.canonicalize_category(hitting_mat_title, "20740", "Furniture & Scratchers") == (
        "50876",
        "Nets, Cages & Mats",
    )
    assert matcher.is_category_plausible_for_text(hitting_mat_title, "50876", "Nets, Cages & Mats")

    treadmill_title = "Electric Treadmill with Auto Incline Foldable 3.5HP Running Machine 61x28x48in Black"
    assert matcher.canonicalize_category(treadmill_title, "262210", "Running Boards & Nerf Bars") == (
        "15280",
        "Treadmills",
    )
    assert matcher.is_category_plausible_for_text(
        "Treadmills for Home, Electric Treadmill with Automatic Incline, Foldable 3.5HP Workout Running Machine Walking, Double Running Board Shock Absorption",
        "15280",
        "Treadmills",
    )

    pantry_title = "Kitchen Hutch Cabinet with Microwave Shelf Freestanding Pantry Storage 63 W Walnut"
    assert matcher.canonicalize_category(pantry_title, "38217", "China Cabinets") == (
        "20487",
        "Cabinets & Cupboards",
    )

    potting_bench_title = "Tall Garden Potting Bench Table with Hutch 65.7 in H Brown Wood Rustic"
    assert matcher.canonicalize_category(potting_bench_title, "38217", "China Cabinets") == (
        "139939",
        "Greenhouses",
    )

    ottoman_title = '32" Round Tufted Ottoman with Solid Wood Legs, Modern Coffee Table with Tufted Buttons, Chenille Fabric Footrest'
    assert matcher.canonicalize_category(ottoman_title, "20490", "Ottomans, Footstools & Poufs") == (
        "20490",
        "Ottomans, Footstools & Poufs",
    )
    assert matcher.is_category_plausible_for_text(ottoman_title, "20490", "Ottomans, Footstools & Poufs")


def test_hall_tree_dimensions_use_vertical_height_not_supplier_length():
    dims = extract_all_dimensions({
        "Product Type": "Hall Tree",
        "Assembled Length (in.)": "76.70",
        "Assembled Width (in.)": "59.00",
        "Assembled Height (in.)": "15.70",
        "Product Weight (lbs.)": "143.30",
    })

    assert dims["length"] == 15.7
    assert dims["width"] == 59.0
    assert dims["height"] == 76.7
    assert dims["weight"] == 143.3


def test_product_dimensions_blob_populates_missing_item_dimensions():
    dims = extract_all_dimensions({
        "Product Dimensions": "157.48'' W x 157.48'' D x 98.43'' H",
        "Item Weight": "26.68 Pounds",
    })

    assert dims["length"] == 157.5
    assert dims["width"] == 157.5
    assert dims["height"] == 98.4
    assert dims["weight"] == 26.68


def test_zero_gravity_indoor_recliner_is_not_forced_to_outdoor_chairs():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Zero Gravity Air Leather Recliner Chair with Vibration and Heating"

    assert matcher._fallback_category(title)[0] == "54235"
    assert matcher.is_category_plausible_for_text(title, "54235", "Chairs")
    assert not matcher.is_category_plausible_for_text(title, "79684", "Outdoor Chairs")


def test_non_leaf_furniture_categories_canonicalize_to_sellable_leaves():
    matcher = object.__new__(EbayCategoryMatcher)

    assert matcher.canonicalize_category("63 Inch Console Table with Drawers", "38205", "Console Tables") == (
        "38204",
        "Tables",
    )
    assert matcher.canonicalize_category("Kitchen Pantry Cabinet with Pull-Out Shelves", "42428", "Pantry Cabinets") == (
        "20487",
        "Cabinets & Cupboards",
    )


def test_coffee_bar_hutch_is_not_forced_to_wine_storage():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Kitchen Hutch Cabinet Farmhouse Buffet Cabinet with Microwave Stand Freestanding Sideboard Coffee Bar Cabinet"

    assert matcher._fallback_category(title)[0] in {"183322", "20487", "38217"}
    assert matcher.is_category_plausible_for_text(title, "20487", "Cabinets & Cupboards")
    assert matcher.canonicalize_category(title, "20689", "Wine Racks & Bottle Holders") != (
        "20689",
        "Wine Racks & Bottle Holders",
    )


def test_ready_benches_are_not_published_as_tables_stools_or_pet_furniture():
    matcher = object.__new__(EbayCategoryMatcher)
    mid_century_bench = (
        "52 Inch Mid Century Bench with Walnut Wood Leg Channeled Upholstered "
        "Footrest Stool for Kitchen Living Room Bedroom Entryway End of Bed"
    )
    dining_bench = (
        "50.25 Inch Upholstered Long Bench Modern Dining Bench with Soft Chenille "
        "Fabric Rubber Wood Legs for Kitchen Table Bedroom Entryway"
    )

    for title in (mid_century_bench, dining_bench):
        assert matcher._fallback_category(title)[0] == "262980"
        assert matcher.canonicalize_category(title, "20740", "Furniture & Scratchers") == (
            "262980",
            "Benches",
        )
        assert matcher.canonicalize_category(title, "103431", "Bar Stools & Stools") == (
            "262980",
            "Benches",
        )
        assert matcher.canonicalize_category(title, "38204", "Tables") == (
            "262980",
            "Benches",
        )
        assert matcher.is_category_plausible_for_text(title, "262980", "Benches")


def test_patio_furniture_set_with_coffee_table_is_not_published_as_indoor_table_or_sofa():
    matcher = object.__new__(EbayCategoryMatcher)
    title = (
        "Luxury Patio Furniture Set with Removable Cushions Coffee Table "
        "L-Shaped Acacia Wood Sofa with Side Table Outdoor Conversation Set"
    )

    assert matcher._fallback_category(title)[0] == "139849"
    assert matcher.canonicalize_category(title, "38204", "Coffee Tables") == (
        "139849",
        "Patio & Garden Furniture Sets",
    )
    assert matcher.canonicalize_category(title, "38208", "Sofas, Armchairs & Couches") == (
        "139849",
        "Patio & Garden Furniture Sets",
    )
    assert matcher.is_category_plausible_for_text(title, "139849", "Patio & Garden Furniture Sets")


def test_coffee_tables_are_not_recategorized_from_sofa_mentions_or_pedestal_bases():
    matcher = object.__new__(EbayCategoryMatcher)
    marble_table = "Marble Pattern Coffee Table Rectangular Living Room Table 39.4 x 21.7 x 11.8 in Gray MDF"
    pedestal_table = "Oval Coffee Table Mid Century Modern Fluted Pedestal Wood Living Room 43.1 in Natural Walnut"

    assert matcher.canonicalize_category(
        marble_table,
        "38204",
        "Coffee Tables",
        "Place this table in front of your sofa for daily living room use.",
    ) == ("38204", "Coffee Tables")
    assert matcher.canonicalize_category(
        marble_table,
        "38208",
        "Sofas, Armchairs & Couches",
        "Place this table in front of your sofa for daily living room use.",
    ) == ("38204", "Tables")
    assert matcher.is_category_plausible_for_text(marble_table, "38204", "Coffee Tables")
    assert matcher.is_category_plausible_for_text(pedestal_table, "38204", "Coffee Tables")
    assert not matcher.is_category_plausible_for_text(pedestal_table, "29514", "Plant Stands")


def test_board_game_table_and_shade_sail_categories_are_not_overgeneralized():
    matcher = object.__new__(EbayCategoryMatcher)

    board_game_table = "Board Game Table with Removable Top Cup Holders for 4-6 Players 63x40in Rustic Brown"
    assert matcher._fallback_category(board_game_table) == ("38204", "Tables")
    assert matcher.canonicalize_category(board_game_table, "54235", "Chairs") == (
        "38204",
        "Tables",
    )
    assert matcher.is_category_plausible_for_text(board_game_table, "38204", "Tables")

    shade_sail = "Hexagonal Shade Sail Canopy UV Block Sun Shade 206x122in Waterproof UPF 50+ Brown PU"
    assert matcher._fallback_category(shade_sail) == ("180997", "Shade Sails")
    assert matcher.canonicalize_category(shade_sail, "180994", "Pergolas") == (
        "180997",
        "Shade Sails",
    )
    assert matcher.is_category_plausible_for_text(shade_sail, "180997", "Shade Sails")


def test_bean_bag_chair_is_not_overgeneralized_as_sofa():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Giant Bean Bag Chair for Adults with Lumbar Pillow Oversized Corduroy Sofa Dark Grey"

    assert matcher._fallback_category(title) == ("48319", "Bean Bags & Inflatables")
    assert matcher.canonicalize_category(title, "38208", "Sofas, Armchairs & Couches") == (
        "48319",
        "Bean Bags & Inflatables",
    )
    assert matcher.is_category_plausible_for_text(title, "48319", "Bean Bags & Inflatables")


def test_dining_sets_are_not_published_as_single_chairs_or_tables():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "5-Piece Dining Set Mid-Century Modern Walnut Black Table and Chair Set"

    assert matcher.canonicalize_category(title, "54235", "Chairs") == (
        "107578",
        "Dining Sets",
    )
    assert matcher.canonicalize_category(title, "38204", "Tables") == (
        "107578",
        "Dining Sets",
    )
    assert matcher.is_category_plausible_for_text(title, "107578", "Dining Sets")


def test_coolers_are_not_published_as_tables():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Portable Insulated Hard Cooler Can Outdoor Ice Chest"

    assert matcher.canonicalize_category(title, "38204", "Tables") == (
        "79691",
        "Ice Chests & Coolers",
    )
    assert matcher.is_category_plausible_for_text(title, "79691", "Ice Chests & Coolers")


def test_existing_offer_update_does_not_preserve_zero_quantity():
    assert RealEbayClient._positive_offer_quantity(0) == 1
    assert RealEbayClient._positive_offer_quantity("0") == 1
    assert RealEbayClient._positive_offer_quantity(5) == 5


def test_existing_offer_update_completes_missing_business_policies():
    class FakePolicyManager:
        def get_default_fulfillment_policy_id(self):
            return None

        def get_default_return_policy_id(self):
            return None

        def get_default_payment_policy_id(self):
            return None

    client = object.__new__(RealEbayClient)
    client.policy_manager = FakePolicyManager()

    policies = client._complete_listing_policies({"eBayPlusIfEligible": False})

    assert policies["eBayPlusIfEligible"] is False
    assert policies["fulfillmentPolicyId"]
    assert policies["returnPolicyId"]
    assert policies["paymentPolicyId"]


def test_delist_sku_reports_failure_when_inventory_delete_fails_without_offers():
    client = object.__new__(RealEbayClient)
    client.get_offers_by_sku = lambda sku: []
    client.delete_inventory_item = lambda sku: False

    result = client.delist_sku("SKU-NO-OFFER")

    assert result["success"] is False
    assert "Delete inventory item SKU-NO-OFFER: FAILED" in result["steps"]


def test_delist_sku_reports_failure_when_offer_delete_fails():
    client = object.__new__(RealEbayClient)
    client.get_offers_by_sku = lambda sku: [
        {"offerId": "OFFER-1", "status": "PUBLISHED"},
    ]
    client.withdraw_offer = lambda offer_id: True
    client.delete_offer = lambda offer_id: False
    client.delete_inventory_item = lambda sku: True

    result = client.delist_sku("SKU-OFFER")

    assert result["success"] is False
    assert result["error"] == "Failed to delete offer OFFER-1"


def test_implausible_item_dimensions_are_rejected():
    assert find_dimension({"Assembled Height (in.)": "1999.99"}, "Height") is None

    errors = measurement_validation_errors({
        "Item Length": ["31.5 in"],
        "Item Width": ["15.8 in"],
        "Item Height": ["2000.0 in"],
    })

    assert "implausible measurement aspect: Item Height=2000.0 in" in errors


def test_analysis_callers_pass_specs_to_optimizer():
    for relative_path in ("server.py", "daily_tasks.py", "batch_analyze.py"):
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8-sig"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "optimize_product_full"
                )
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "optimize_product_full_with_timeout"
                )
            )
        ]
        assert calls, f"{relative_path} should call optimize_product_full"
        assert any(
            keyword.arg == "specs"
            for call in calls
            for keyword in call.keywords
        ), f"{relative_path} must pass specs into optimize_product_full"


def test_analysis_callers_use_hard_qwen_timeout_wrapper():
    for relative_path in ("server.py", "daily_tasks.py", "batch_analyze.py"):
        source = (ROOT / relative_path).read_text(encoding="utf-8-sig")

        assert "optimize_product_full_with_timeout" in source


def test_batch_publish_does_not_copy_package_dimensions_to_assembled_attributes():
    source = (ROOT / "batch_publish.py").read_text(encoding="utf-8-sig")

    assert "from package dims" not in source
    assert "Fallback: {attr_key}" not in source


def test_batch_publish_uses_stable_image_preparation_not_forced_eps_upload():
    source = (ROOT / "batch_publish.py").read_text(encoding="utf-8-sig")

    assert "_prepare_inventory_image_urls" in source
    assert "upload_images_to_eps(raw_images" not in source


def test_replace_description_measurements_updates_overall_weight_rows():
    html = "<table><tr><td>Overall Weight</td><td>Not specified</td></tr></table>"

    updated = replace_description_measurements(html, weight=113.09)

    assert "<td>Overall Weight</td><td>113.09 lbs</td>" in updated
    assert "Not specified" not in updated


def test_description_weight_placeholder_uses_explicit_package_weight_label():
    html = '<table><tr><td>Weight</td><td>Not specified - see package details</td></tr></table>'

    updated = replace_description_weight_placeholder_with_package_weight(html, 107.9)

    assert "<td>Package Weight</td><td>107.9 lbs</td>" in updated
    assert "Not specified" not in updated


def test_placeholder_item_specifics_are_replaced_or_removed_before_publish():
    aspects = {
        "Set Includes": ["See Description"],
        "Item Weight": ["33.0 lbs"],
        "Weight": ["Not specified"],
        "Brand": ["AquaVerve"],
    }

    sanitized = sanitize_placeholder_aspects(
        aspects,
        title="Outdoor Steel Slat Dining Table Square Backyard Bistro Table",
        category_id="112590",
    )

    assert sanitized["Set Includes"] == ["Table"]
    assert sanitized["Weight"] == ["33.0 lbs"]
    assert "See Description" not in str(sanitized)
    assert "Not specified" not in str(sanitized)


def test_set_includes_inference_never_returns_generic_placeholder():
    assert infer_set_includes("Mid Century Modern Glass Coffee Table") == "Table"
    assert infer_set_includes("Kids Activity Table and Chair Set") == "Table & Chairs"
    assert infer_set_includes("Set of 2 Mid Century Upholstered Dining Chairs") == "Chairs"
    assert infer_set_includes("Simple Storage Cabinet") is None


def test_publish_aspect_completion_does_not_add_sofa_set_to_bean_bags():
    from src.utils.publish_aspect_completion import complete_publish_aspects

    completed = complete_publish_aspects(
        {"Brand": ["AquaVerve"], "Type": ["Beanbag"]},
        title="Giant Bean Bag Chair Oversized Lazy Sofa",
        category_id="48319",
        attrs={
            "Assembled Length (in.)": "39.4",
            "Assembled Width (in.)": "47.2",
            "Assembled Height (in.)": "29.9",
        },
        ensure_required_dimensions=False,
    )

    assert "Set Includes" not in completed


def test_publish_aspect_completion_sets_dining_set_item_count_from_title():
    from src.utils.publish_aspect_completion import complete_publish_aspects

    completed = complete_publish_aspects(
        {"Brand": ["AquaVerve"], "Type": ["Dining Set"]},
        title="5-Piece Outdoor Acacia Wood Round Dining Set with 4 Barrel Chairs",
        category_id="107578",
        attrs={
            "Assembled Length (in.)": "44.5",
            "Assembled Width (in.)": "44.5",
            "Assembled Height (in.)": "29.5",
        },
        ensure_required_dimensions=False,
    )

    assert completed["Number of Items in Set"] == ["5"]
    assert completed["Number of Pieces"] == ["5"]


def test_publish_autofix_refuses_generic_missing_aspect_placeholder():
    aspects = {}

    fixed = try_fix_publish_error(
        "The item specific Set Includes is missing",
        aspects,
        "38204",
        {},
        ["Item Length", "Item Width", "Item Height"],
    )

    assert fixed is False
    assert aspects == {}


def test_category_matcher_keyword_hints_cover_current_nonterminal_category_leaks():
    matcher = object.__new__(EbayCategoryMatcher)

    trellis_title = "2 Pack Metal Garden Trellis 86.7 x 19.7 Rustproof Trellis for Climbing Plants Outdoor Flower Support"
    assert matcher.get_keyword_category_hint(trellis_title) == ("43538", "Trellises")
    assert matcher.is_category_plausible_for_text(trellis_title, "43538", "Trellises")
    assert not matcher.is_category_plausible_for_text(trellis_title, "20740", "Furniture & Scratchers")

    arch_title = "Metal Garden Arch with Gate 79.5 Wide x 86.6 High Climbing Plants Support Rose Arch Outdoor Black"
    assert matcher.get_keyword_category_hint(arch_title) == ("180993", "Arbors & Arches")
    assert matcher.is_category_plausible_for_text(arch_title, "180993", "Arbors & Arches")
    assert not matcher.is_category_plausible_for_text(arch_title, "20740", "Furniture & Scratchers")

    luggage_title = "14 20 2 pcs set in ABS Spinner Wheel Luggage with Combination Lock 14 Cosmetic Case Silver"
    assert matcher.get_keyword_category_hint(luggage_title) == ("16080", "Luggage")
    assert matcher.is_category_plausible_for_text(luggage_title, "16080", "Luggage")
    assert not matcher.is_category_plausible_for_text(luggage_title, "20740", "Furniture & Scratchers")

    arcade_title = "Foldable Basketball Arcade Game Dual Shot with LED Electronic Scorer 4 Balls Pump Included"
    assert matcher.get_keyword_category_hint(arcade_title) == ("36278", "Other Indoor Games")
    assert matcher.is_category_plausible_for_text(arcade_title, "36278", "Other Indoor Games")
    assert not matcher.is_category_plausible_for_text(arcade_title, "20740", "Furniture & Scratchers")


def test_arbor_titles_with_plant_stands_still_allow_arbors_category():
    matcher = object.__new__(EbayCategoryMatcher)
    title = "Metal Garden Arch with Two Plant Stands for Climbing Plants Rose Arbor Outdoor"

    assert matcher.is_category_plausible_for_text(title, "180993", "Arbors & Arches")
    assert not matcher.is_category_plausible_for_text(title, "29514", "Plant Stands")


def test_ready_draft_category_choice_does_not_keep_protected_but_implausible_category():
    class FakeMatcher:
        def get_category_and_aspects(self, title, aspects, description):
            return "180993", "Arbors & Arches", aspects

        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name=None):
            return category_id == "180993"

    category_id, category_name, _ = choose_ready_category(
        FakeMatcher(),
        "Metal Garden Arch with Gate 79.5 Wide x 86.6 High Climbing Plants Support Rose Arch Outdoor Black",
        "Metal Garden Arch with Double Gate 79.5 in Wide x 86.6 in High Climbing Plants",
        "",
        {"Brand": ["AquaVerve"]},
        "262216",
        "Roof Racks & Cross Bars",
    )

    assert (category_id, category_name) == ("180993", "Arbors & Arches")
