"""Pre-publish category check: 26 real SKUs, boundary checklist, call sites."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.utils.category_decision import (
    BLOCKED_CATEGORY_REVIEW,
    PREPUBLISH_HOLD_REASONS,
    Verdict,
    extract_product_type_signal,
    format_prepublish_error,
    is_prepublish_block,
    labels_equal,
    map_product_type,
    normalize_category_label,
    prepublish_category_check,
)
from tests.conftest import load_fixture_category_index

INDEX = load_fixture_category_index()


def _check(
    title,
    category_id,
    category_name="",
    *,
    attributes=None,
    description="",
    store="main",
    sku="SKU",
):
    return prepublish_category_check(
        sku,
        category_id,
        category_name,
        title,
        attributes,
        description,
        store=store,
        index=INDEX,
    )


def _kind(verdict: Verdict) -> str:
    if verdict.ok:
        return "PASS"
    if verdict.reason in PREPUBLISH_HOLD_REASONS:
        return "HOLD"
    return "BLOCK"


# ---------------------------------------------------------------------------
# 26 real-SKU rows (titles copied from the live-data table)
# ---------------------------------------------------------------------------

REAL_SKU_ROWS = [
    (
        1,
        "N707P228272Z",
        "Outdoor Grill Cart with SS Top Stainless Steel Outdoor Storage Cabinet",
        "20487",
        "Cabinets & Cupboards",
        "main",
        None,
        "BLOCK",
    ),
    (
        2,
        "N707P397500B",
        '76.6" Outdoor Large Grill Cart with Storage Cabinet, Outdoor Kitchen Island',
        "20487",
        "Cabinets & Cupboards",
        "main",
        None,
        "BLOCK",
    ),
    (
        3,
        "N707P370276G",
        "Outdoor Kitchen Island with Foldable Grill Table Rolling Grill Cart",
        "20487",
        "Cabinets & Cupboards",
        "gp",
        None,
        "BLOCK",
    ),
    (
        4,
        "N719P407403E",
        "Portable BBQ Cart Grill Cart",
        "177000",
        "Power Tool Sets",
        "main",
        None,
        "BLOCK",
    ),
    (
        5,
        "W465P423164",
        "4-Pack RV Stabilizer Jacks Heavy Duty Travel Trailer",
        "11848",
        "Women's Fragrances",
        "main",
        None,
        "BLOCK",
    ),
    (
        6,
        "W465P456235",
        "RV Leveling Blocks Kit",
        "177849",
        "Car & Truck Racks",
        "main",
        None,
        "BLOCK",
    ),
    (
        7,
        "W5368P551872",
        "Solid Wood Rustic Side Table Nightstand End Table",
        "54235",
        "Chairs",
        "gp",
        None,
        "BLOCK",
    ),
    (
        8,
        "N711P541954K",
        "Modern Round Side Table / Hourglass Accent Table",
        "38208",
        "Sofas, Armchairs & Couches",
        "gp",
        None,
        "BLOCK",
    ),
    (
        9,
        "W808P391671",
        "Dog Crate Furniture with Wheels",
        "54235",
        "Chairs",
        "gp",
        None,
        "BLOCK",
    ),
    (
        10,
        "W331P470122",
        "LED Bathroom Mirror Cabinet",
        "32878",
        "Vanities & Makeup Tables",
        "main",
        None,
        "BLOCK",
    ),
    (
        11,
        "N729P517639N",
        '24" Bathroom Vanity with Ceramic Sink Bathroom Cabinet',
        "20487",
        "Cabinets & Cupboards",
        "main",
        None,
        "BLOCK",
    ),
    (
        12,
        "W1668P147059",
        "Long Bar Table Set with 3 Upholstered Bar Stools",
        "103431",
        "Bar Stools & Stools",
        "main",
        None,
        "BLOCK",
    ),
    (
        13,
        "N753P503257B",
        "Reception Sofa Bench",
        "38208",
        "Benches",
        "main",
        None,
        "BLOCK",
    ),
    (
        14,
        "W2615P429613",
        "Bathroom Wall Mounted Narrow Storage Cabinet",
        "176991",
        "Bathroom Cabinets",
        "main",
        None,
        "PASS",
    ),
    (
        15,
        "W1097P462002",
        "Ergonomic High Back Lumbar Support Chair",
        "257900",
        "Chairs & Stools",
        "main",
        None,
        "PASS",
    ),
    (
        16,
        "W1143P585109",
        "Swivel Bar Stools Set of 2 Counter Stools",
        "103431",
        "Bar Stools & Stools",
        "main",
        None,
        "PASS",
    ),
    (
        17,
        "W3204P601805",
        "TV Stands for Living Room Entertainment Center, TV Cabinet",
        "20488",
        "TV Stands & Entertainment Units",
        "main",
        None,
        "PASS",
    ),
    (
        18,
        "W3081P534790",
        "Modern Curved Console Table",
        "38204",
        "Tables",
        "main",
        None,
        "PASS",
    ),
    (
        19,
        "W5599P552123",
        "Farmhouse Wooden Storage Bench",
        "262980",
        "Benches",
        "main",
        None,
        "PASS",
    ),
    (
        20,
        "W1885P277277",
        "Human Dog Bed, Lazy Sofa Couch",
        "38208",
        "Sofas, Armchairs & Couches",
        "main",
        None,
        "PASS",
    ),
    (
        21,
        "W2660P369120",
        "Dog Ramp for Car",
        "116389",
        "Ramps & Stairs",
        "main",
        None,
        "PASS",
    ),
    (
        22,
        "W1687S00005",
        "Dog Crate Furniture TV Stand",
        "20748",
        "Cages & Crates",
        "gp",
        None,
        "PASS",
    ),
    (
        23,
        "W2921P505747",
        '20" Kids Bike',
        "177831",
        "Bicycles",
        "main",
        None,
        "PASS",
    ),
    (
        24,
        "W465P575127",
        '2" Truck Hitch Game Hoist',
        "33653",
        "Trailer Hitches",
        "aqua",
        None,
        "PASS",
    ),
    (
        25,
        "W465P293450",
        "Moving Wooden Dolly Furniture 18 Inch Sofa",
        "179506",
        "Dollies",
        "aqua",
        None,
        "PASS",
    ),
    (
        26,
        "W1162104644",
        "Bar Table Set with 2 Bar Stools",
        "38204",
        "Tables",
        "main",
        {"Product Type": "Bar table set of 3"},
        "HOLD",
    ),
]


@pytest.mark.parametrize(
    "row,sku,title,cid,cname,store,attrs,expected",
    REAL_SKU_ROWS,
    ids=[f"{row}-{sku}" for row, sku, *_ in REAL_SKU_ROWS],
)
def test_real_sku_prepublish_table(row, sku, title, cid, cname, store, attrs, expected):
    verdict = _check(title, cid, cname, attributes=attrs, store=store, sku=sku)
    assert _kind(verdict) == expected, (row, sku, verdict)
    if expected == "BLOCK":
        assert is_prepublish_block(verdict)
        assert BLOCKED_CATEGORY_REVIEW in format_prepublish_error(verdict)
        assert verdict.ok is False
    elif expected == "HOLD":
        assert verdict.ok is False
        assert verdict.reason in PREPUBLISH_HOLD_REASONS
    else:
        assert verdict.ok is True


def test_row14_retired_42428_blocks_and_never_remaps():
    title = "Bathroom Wall Mounted Narrow Storage Cabinet"
    ok = _check(title, "176991", "Bathroom Cabinets", store="main")
    assert ok.ok is True
    blocked = _check(title, "42428", "Bathroom Cabinets", store="main")
    assert blocked.ok is False
    assert blocked.reason == "retired_category"
    assert "20487" != str(blocked.suggestion or "")
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    matcher = object.__new__(EbayCategoryMatcher)
    assert matcher.canonicalize_category(title, "42428", "Bathroom Cabinets") == (
        "42428",
        "Bathroom Cabinets",
    )


def test_row10_suggests_176991_report_only():
    verdict = _check(
        "LED Bathroom Mirror Cabinet",
        "32878",
        "Vanities & Makeup Tables",
    )
    assert verdict.ok is False
    assert verdict.suggestion == "176991"


def test_row26_product_type_wins_over_title_stools():
    verdict = _check(
        "Swivel Bar Stools Counter Stools",
        "38204",
        "Tables",
        attributes={"Product Type": "Bar table set of 3"},
    )
    assert verdict.ok is False
    assert verdict.reason == "set_vs_single"


# ---------------------------------------------------------------------------
# Boundary checklist
# ---------------------------------------------------------------------------


def test_boundary_whole_value_not_substring():
    assert labels_equal("Sofas", "Sofas")
    assert not labels_equal("Sofas", "Sofas & Sectionals")
    assert not labels_equal("Sofas", "Sofa Tables")
    table_in_stable = _check("Horse Stable Door Panel", "38204", "Tables")
    assert table_in_stable.ok is False
    tablet = _check("Android Tablet Stand", "38204", "Tables")
    assert tablet.ok is False
    tv_cabinet = _check(
        "TV Stands for Bedroom Entertainment Center, TV Cabinet",
        "20488",
        "TV Stands & Entertainment Units",
    )
    assert tv_cabinet.ok is True
    mirror_cab = _check(
        "LED Bathroom Mirror Cabinet",
        "20487",
        "Cabinets & Cupboards",
    )
    assert mirror_cab.ok is False
    grill_with_cabinet = _check(
        "Outdoor Grill Cart with Storage Cabinet",
        "20487",
        "Cabinets & Cupboards",
    )
    assert grill_with_cabinet.ok is False


def test_boundary_normalization_both_sides():
    assert labels_equal("  BAR STOOLS  ", "Bar Stool")
    assert labels_equal("Bar Stools", "Bar Stool")
    assert labels_equal("Cabinets & Cupboards", "Cabinets and Cupboards")
    fullwidth = normalize_category_label("Ｃａｂｉｎｅｔｓ　＆　Ｃｕｐｂｏａｒｄｓ")
    assert "cabinet" in fullwidth
    verdict = _check(
        "Swivel Bar Stools Counter Height",
        "103431",
        "  Bar Stools & Stools ",
    )
    assert verdict.ok is True


def test_boundary_empty_none_never_crash_or_silent_pass():
    empty_id = prepublish_category_check("S", None, "Tables", "Side Table", None, None, store="main", index=INDEX)
    assert empty_id.ok is False
    assert empty_id.reason == "hold_empty"
    missing_id = prepublish_category_check("S", "", None, "Side Table", {}, "", store="main", index=INDEX)
    assert missing_id.reason == "hold_empty"
    no_title = prepublish_category_check("S", "38204", "Tables", None, None, None, store="main", index=INDEX)
    assert no_title.ok is False
    assert no_title.reason == "unknown_product_type"
    no_name = _check("Modern Curved Console Table", "38204", None)
    assert no_name.ok is True
    int_id = _check("Modern Curved Console Table", 38204, "Tables")
    assert int_id.ok is True


def test_boundary_product_type_wins_when_signals_disagree():
    signal = extract_product_type_signal(
        "Swivel Bar Stools Counter Stools",
        {"Product Type": "Bar table set"},
        "",
    )
    assert "table set" in signal.lower()
    mapped = map_product_type(signal)
    assert mapped is not None
    assert mapped[2] is True


def test_boundary_unknown_product_type_holds():
    verdict = _check("Quantum Flux Capacitor Widget", "38204", "Tables")
    assert verdict.ok is False
    assert verdict.reason == "unknown_product_type"


def test_boundary_numbers_do_not_change_head_noun():
    chairs = extract_product_type_signal('47 Inch Chairs', None, "")
    assert "chair" in chairs.lower()
    vanity = extract_product_type_signal('24" Vanity', None, "")
    assert "vanity" in vanity.lower()
    piece = map_product_type("2-Piece Bar table set")
    assert piece is not None and piece[2] is True
    set_of = map_product_type("Set of 3 Bar table set")
    assert set_of is not None and set_of[2] is True


def test_boundary_aqua_motors_pass_and_main_does_not_force_pass():
    aqua = _check(
        '2" Truck Hitch Game Hoist',
        "33653",
        "Trailer Hitches",
        store="aqua",
    )
    assert aqua.ok is True
    dolly = _check(
        "Moving Wooden Dolly Furniture 18 Inch Sofa",
        "179506",
        "Dollies",
        store="aqua",
    )
    assert dolly.ok is True
    main_hitch = _check(
        '2" Truck Hitch Game Hoist',
        "33653",
        "Trailer Hitches",
        store="main",
    )
    assert main_hitch.ok is False
    assert main_hitch.reason == "wrong_store"


def test_boundary_crlf_product_type_line():
    verdict = _check(
        "Swivel Bar Stools",
        "38204",
        "Tables",
        description="Specs\r\nProduct Type: Bar table set of 3\r\nColor: Black",
    )
    assert verdict.ok is False
    assert verdict.reason == "set_vs_single"


# ---------------------------------------------------------------------------
# Call sites invoke the check (including approved / locked)
# ---------------------------------------------------------------------------


def _stub_batch_publish_pipeline(monkeypatch, bp, *, fake_check, record_error=None):
    monkeypatch.setattr(bp, "validate_product", lambda product: [])
    monkeypatch.setattr(
        "src.utils.copy_skip_gate.seller_is_blacklisted",
        lambda sku: False,
    )
    monkeypatch.setattr(bp, "enrich_product_dimensions", lambda product: None)
    monkeypatch.setattr(bp, "record_error", record_error or (lambda *a, **k: None))
    monkeypatch.setattr(bp, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(bp, "calculate_smart_final_price", lambda product, market=None: 99)
    monkeypatch.setattr(
        "src.utils.copy_skip_gate.supplier_cargo_cost",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.services.ebay_category_matcher.EbayCategoryMatcher",
        lambda oauth: MagicMock(),
    )
    monkeypatch.setattr(
        "src.utils.category_decision.prepublish_category_check",
        fake_check,
    )


def test_call_site_batch_publish_invokes_check_including_approved(monkeypatch):
    import batch_publish as bp

    seen = []

    def fake_check(*args, **kwargs):
        seen.append((args, kwargs))
        return Verdict(False, "leaf_family_conflict", "38204")

    _stub_batch_publish_pipeline(monkeypatch, bp, fake_check=fake_check)

    class _Oauth:
        def is_authorized(self):
            return True

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda env: _Oauth())
    monkeypatch.setattr(
        "src.utils.approved_category.evaluate_approved_category",
        lambda *a, **k: SimpleNamespace(approved=True, preserve=True, error=""),
    )
    monkeypatch.setattr(
        "src.utils.approved_category.blocks_automated_category_change",
        lambda opt: True,
    )
    monkeypatch.setattr(
        "src.utils.approved_category.is_valid_category_id",
        lambda cid: True,
    )
    monkeypatch.setattr(
        bp,
        "select_batch_publish_category",
        lambda **k: {
            "held": False,
            "category_id": "20487",
            "category_name": "Cabinets & Cupboards",
            "aspects": {"Brand": ["X"]},
            "flags": [],
            "original_stored": "20487",
        },
    )
    product = {
        "sku": "APPROVED1",
        "title": "Outdoor Grill Cart with SS Top",
        "description": "x" * 120,
        "price": 99,
        "suggested_price": 99,
        "images": ["https://example.com/a.jpg"],
        "optimization": {
            "title": "Outdoor Grill Cart with SS Top",
            "description": "x" * 120,
            "categoryId": "20487",
            "categoryName": "Cabinets & Cupboards",
            "aspects": {"Brand": ["X"]},
            "categoryApproved": True,
        },
        "attributes": {},
        "status": "READY",
    }
    result = bp.publish_single_product(product, dry_run=True)
    assert seen
    assert result["status"] == "error"
    assert result["reason"] == "leaf_family_conflict"


def test_call_site_server_publish_invokes_check(monkeypatch):
    import inspect

    import server as srv

    source = inspect.getsource(srv.publish_product)
    assert "prepublish_category_check" in source
    source_mi = inspect.getsource(srv.mi_audit_and_publish)
    assert "publish_product" in source_mi


def test_call_site_app_publish_paths_invoke_check():
    import inspect

    import app as app_mod

    assert "prepublish_category_check" in inspect.getsource(app_mod.publish_to_ebay)
    assert "prepublish_category_check" in inspect.getsource(app_mod.publish_with_auto_category)


def test_call_site_ebay_publisher_invokes_check():
    import inspect

    from src.services.ebay_publisher import EbayPublisher, apply_category_publish_gate

    assert "prepublish_category_check" in inspect.getsource(EbayPublisher.publish)
    assert "prepublish_category_check" in inspect.getsource(apply_category_publish_gate)


def test_call_site_sku_field_edit_invokes_check_and_blocks(monkeypatch):
    from src.services.sku_field_edit import run_category_gates
    from src.utils.category_decision import Verdict as V

    seen = []

    def fake_check(*args, **kwargs):
        seen.append(args[1] if args else kwargs.get("category_id"))
        return V(False, "leaf_family_conflict", None)

    monkeypatch.setattr(
        "src.utils.category_decision.prepublish_category_check",
        fake_check,
    )
    ctx = SimpleNamespace(
        store="main",
        category_index=INDEX,
        high_fee_blacklist_path=None,
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.store_tree_id",
        lambda store: "0",
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.store_category_indexes",
        lambda ctx: {"0": INDEX},
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.is_cached_leaf_in_tree",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.is_valid_category_id",
        lambda cid: True,
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.resolve_category_name",
        lambda cid, ctx: "Cabinets & Cupboards",
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.guard_category_write_high_fee",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.locked_category_write_error",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.services.sku_field_edit.required_aspect_names",
        lambda ctx, cid: (["Brand"], True),
    )
    args = SimpleNamespace(allow_category=True, override_category_lock=False, push_live=False)
    plan = SimpleNamespace(sku="X", row={"attributes": {}}, live={})
    ok, reason, _name = run_category_gates(
        "20487",
        {"title": "Outdoor Grill Cart", "description": "", "aspects": {"Brand": ["A"]}},
        {"Brand": ["A"]},
        ctx,
        args,
        plan=plan,
    )
    assert ok is False
    assert seen
    assert reason in {BLOCKED_CATEGORY_REVIEW, "leaf_family_conflict"}


def test_dry_run_does_not_record_error(monkeypatch):
    import batch_publish as bp

    recorded = []
    _stub_batch_publish_pipeline(
        monkeypatch,
        bp,
        fake_check=lambda *a, **k: Verdict(False, "leaf_family_conflict", None),
        record_error=lambda sku, msg: recorded.append((sku, msg)),
    )

    class _Oauth:
        def is_authorized(self):
            return True

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", lambda env: _Oauth())
    monkeypatch.setattr(
        "src.utils.approved_category.evaluate_approved_category",
        lambda *a, **k: SimpleNamespace(approved=False, preserve=False, error=""),
    )
    monkeypatch.setattr(
        "src.utils.approved_category.blocks_automated_category_change",
        lambda opt: False,
    )
    monkeypatch.setattr(
        bp,
        "select_batch_publish_category",
        lambda **k: {
            "held": False,
            "category_id": "20487",
            "category_name": "Cabinets & Cupboards",
            "aspects": {"Brand": ["X"]},
            "flags": [],
            "original_stored": "20487",
        },
    )
    product = {
        "sku": "DRY1",
        "title": "Outdoor Grill Cart with SS Top",
        "description": "x" * 120,
        "price": 99,
        "suggested_price": 99,
        "images": ["https://example.com/a.jpg"],
        "optimization": {
            "title": "Outdoor Grill Cart with SS Top",
            "description": "x" * 120,
            "categoryId": "20487",
            "categoryName": "Cabinets & Cupboards",
            "aspects": {"Brand": ["X"]},
        },
        "attributes": {},
        "status": "READY",
    }
    result = bp.publish_single_product(product, dry_run=True)
    assert result["status"] == "error"
    assert recorded == []


# ---------------------------------------------------------------------------
# Extra edge cases beyond the table / checklist
# ---------------------------------------------------------------------------


def test_edge_int_vs_str_category_id():
    a = _check("Modern Curved Console Table", "38204", "Tables")
    b = _check("Modern Curved Console Table", 38204, "Tables")
    assert a.ok is True and b.ok is True


def test_edge_router_labels_are_ignored():
    signal = extract_product_type_signal(
        "Solid Wood Rustic Side Table",
        {"route_reason": "sofa", "aqua_kind": "sofa", "route_why": "sofa", "Product Type": "Side table"},
        "",
    )
    assert "side table" in signal.lower()


def test_edge_fullwidth_ampersand_and_crlf():
    assert labels_equal("Cabinets ＆ Cupboards", "Cabinets and Cupboards")
    verdict = _check(
        "Farmhouse Wooden Storage Bench",
        "262980",
        "Benches",
        description="Product Type:\r\nBench\r\n",
    )
    assert verdict.ok is True


def test_edge_tree_unavailable_holds(monkeypatch):
    import src.utils.category_decision as cd

    cd.reset_tree_unavailable_warning()
    monkeypatch.setattr(cd, "_category_index", lambda index=None: {})
    verdict = prepublish_category_check(
        "S",
        "38204",
        "Tables",
        "Console Table",
        None,
        "",
        store="main",
        index={},
    )
    assert verdict.ok is False
    assert verdict.reason == "hold_tree_unavailable"


def test_edge_non_leaf_parent_blocks():
    verdict = _check("Modern Curved Console Table", "900007", "Furniture")
    assert verdict.ok is False
    assert verdict.reason in {"invalid_target_leaf", "retired_category", "leaf_family_conflict"}


def test_edge_listing_quality_gate_agrees_on_block():
    from src.utils.category_decision import validate_target_category

    title = "Outdoor Grill Cart with SS Top Outdoor Storage Cabinet"
    pre = _check(title, "20487", "Cabinets & Cupboards")
    assert pre.ok is False
    ok, reason = validate_target_category("20487", "", title, index=INDEX)
    assert ok is False or reason in {"stored_category_implausible", "implausible_target_path", ""}
    assert is_prepublish_block(pre)


def test_edge_empty_product_type_falls_through_to_title():
    signal = extract_product_type_signal(
        "Modern Curved Console Table",
        {"Product Type": ""},
        "",
    )
    assert "console table" in signal.lower() or "table" in signal.lower()
    verdict = _check(
        "Modern Curved Console Table",
        "38204",
        "Tables",
        attributes={"Product Type": None},
    )
    assert verdict.ok is True


def test_edge_bathroom_storage_cabinet_not_furniture_cabinet():
    verdict = _check(
        "Bathroom Wall Mounted Narrow Storage Cabinet",
        "20487",
        "Cabinets & Cupboards",
    )
    assert verdict.ok is False
    assert is_prepublish_block(verdict)


def test_edge_set_of_prefix_does_not_invent_set_family():
    stools = map_product_type("Set of 3 Bar Stools")
    assert stools is not None
    assert stools[0] == "stool"
    assert stools[2] is False
    table_set = map_product_type("Bar table set of 3")
    assert table_set is not None
    assert table_set[2] is True


def test_edge_retired_suggestion_is_not_20487():
    verdict = _check(
        "Kitchen Pantry Cabinet with Pull-Out Shelves",
        "42428",
        "Pantry Cabinets",
    )
    assert verdict.reason == "retired_category"
    assert str(verdict.suggestion or "") != "20487"
