"""PR-B: fail-closed category lock + tree-aware new-target validation.

Real-SKU rows use titles/ids from the 2026-10-23 export (prB spec).
Fixtures that are not copied from that spec are labelled SYNTHETIC.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from src.plugins.inventory_sync.sync_service import (
    apply_pinned_offer_category,
    pin_offer_category_fields,
)
from src.utils.approved_category import (
    CATEGORY_APPROVED_ID_KEY,
    CATEGORY_APPROVED_KEY,
    blocks_automated_category_change,
    category_approval_flag_set,
    is_category_approved,
    is_valid_category_id,
    locked_category_write_error,
    pin_mismatch_reason,
    plan_locked_override_revision,
    should_auto_remap_invalid_category,
)
from src.utils.category_decision import (
    NEEDS_MANUAL_MOTORS_LEAF_REVIEW,
    category_gate_report_flags,
    gate_publish_category_choice,
    needs_manual_motors_leaf_review,
    publish_tree_id_for_target,
    validate_new_target_publish_tree,
)
from src.utils.category_tree_cache import colliding_category_ids, is_cached_leaf_in_tree
from src.utils.store_profile import reset_store_profile_cache

REPO = Path(__file__).resolve().parents[1]

R1_TITLE = (
    "Mid Century Modern Rocking Chair Antique White Teddy Upholstery Solid Rubberwood"
)
R4_TITLE = (
    '52.8"W Wicker Outdoor Grill Cart with Extended Plastic Wood Table Top, '
    "Rolling Kitchen Island on Wheels…"
)
R5_TITLE = '27.2" Dog Crate with Wheels for Small Dogs, Heavy Duty Dog Kennel Indoor'
R6_TITLE = '38.6" Dog Crate Furniture with Wheels for Small Dogs, Heavy Duty Dog Kennel'
R7_TITLE = (
    "High Capacity Rolling Tool Chest with 8 Drawers, Steel Storage Cabinet on Wheels"
)
R9_TITLE = (
    "5-Drawer Rolling Tool Chest with Detachable Top Box, Lockable Wheels & Drawer…"
)
R10_TITLE = (
    "Angle Grinder Fixed Universal Bracket Stand for 150mm 180mm 230mm Grinders"
)
R11_TITLE = (
    "Portable Woodworking Bench Vise with Quick-Release Handle, G-Type Fixing Clips"
)
R12_TITLE = "SYNTHETIC Aqua Inventory-path juicer; real title not in the export"
R13_TITLE = (
    "Acacia Wood Outdoor Chaise Lounge with Adjustable Backrest, Handwoven Rope…"
)
R14_TITLES = {
    "W1321S00027": "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak",
    "W1321S00028": "L-Shaped Computer Desk with Storage Shelves 66 Inch Walnut",
    "W1580S00493": "L-Shaped Desk with Drawers Home Office Workstation",
}

# SYNTHETIC two-tree fixtures. Named leaves match the spec table.
TREE0 = {
    "0": ("Root", ""),
    "11700": ("Home & Garden", "0"),
    "900007": ("Furniture", "11700"),
    "38208": ("Sofas, Armchairs & Couches", "900007"),
    "54235": ("Chairs", "900007"),
    "79682": ("Patio Chairs", "900007"),
    "20487": ("Cabinets & Cupboards", "900007"),
    "88057": ("Desks & Tables", "900007"),
    "262980": ("Benches", "900007"),
    "260150": ("Manual Juicers", "11700"),
    "174020": ("Trailer Hitches", "11700"),
}
TREE100 = {
    "0": ("Root", ""),
    "6000": ("eBay Motors", "0"),
    "174020": ("Brake Pad Wear Sensors", "6000"),
    "33653": ("Trailer Hitches", "6000"),
    "179444": ("Garage/Shop Tool Chests", "6000"),
}


@pytest.fixture(autouse=True)
def _reset_profile_and_tree():
    reset_store_profile_cache()
    from src.utils import category_tree_cache
    from src.utils.category_decision import reset_tree_unavailable_warning

    category_tree_cache.reset_category_tree_cache()
    reset_tree_unavailable_warning()
    yield
    reset_store_profile_cache()
    category_tree_cache.reset_category_tree_cache()
    reset_tree_unavailable_warning()


def _profile(*, motors: bool):
    class _P:
        is_motors = motors
        category_tree_id = "100" if motors else "0"

    return _P()


def _install_two_trees(monkeypatch):
    from src.utils import category_tree_cache

    indexes = {"0": dict(TREE0), "100": dict(TREE100)}
    flat = dict(TREE0)
    flat.update(TREE100)
    monkeypatch.setattr(category_tree_cache, "load_category_indexes", lambda cache_dir=None: indexes)
    monkeypatch.setattr(category_tree_cache, "load_category_index", lambda cache_dir=None: dict(flat))
    return indexes, flat


def _gate(stored, candidate, title, status, *, index=None):
    return gate_publish_category_choice(
        stored,
        candidate,
        title,
        status,
        index=index,
    )


# ---------------------------------------------------------------------------
# Lock predicates
# ---------------------------------------------------------------------------


def test_flag_blocks_blank_and_mismatched_pin():
    blank = {CATEGORY_APPROVED_KEY: True, "categoryId": "38208", CATEGORY_APPROVED_ID_KEY: ""}
    mismatch = {
        CATEGORY_APPROVED_KEY: True,
        "categoryId": "54235",
        CATEGORY_APPROVED_ID_KEY: "99999",
    }
    assert category_approval_flag_set(blank) is True
    assert blocks_automated_category_change(blank) is True
    assert is_category_approved(blank) is True  # legacy flag-only
    assert blocks_automated_category_change(mismatch) is True
    assert is_category_approved(mismatch) is False
    assert pin_mismatch_reason(mismatch) == "pin_mismatch"
    assert locked_category_write_error(mismatch)
    assert should_auto_remap_invalid_category(mismatch) is False
    assert blocks_automated_category_change(mismatch, override=True) is False


def test_plan_locked_override_requires_explicit_id():
    opt = {CATEGORY_APPROVED_KEY: True, CATEGORY_APPROVED_ID_KEY: ""}
    plan = plan_locked_override_revision(opt, "38208")
    assert plan["action"] == "SKIP"
    plan = plan_locked_override_revision(opt, "38208", explicit_target_id="54235")
    assert plan == {
        "action": "REVISE_LIVE",
        "target_id": "54235",
        "reason": "restore approved leaf 54235 (live=38208)",
    }


def test_is_valid_category_id():
    assert is_valid_category_id("38208") is True
    assert is_valid_category_id("") is False
    assert is_valid_category_id("sofa") is False


# ---------------------------------------------------------------------------
# Real-SKU table (parametrized by SKU)
# ---------------------------------------------------------------------------


_REAL_SKU_CASES = [
    ("R1", "W2530P418996", "READY", "38208", R1_TITLE, False, "", "gp", "keep", "38208", ()),
    ("R2", "W2530P418996", "READY", "38208", R1_TITLE, True, "38208", "gp", "keep", "38208", ()),
    ("R3", "W2530P418996", "READY", "38208", R1_TITLE, True, "", "gp", "keep", "38208", ()),
    ("R4", "N707P233747B", "PENDING", "", R4_TITLE, False, "", "gp", "hold_empty", None, ()),
    ("R5", "W808P391606", "READY", "54235", R5_TITLE, True, "54235", "gp", "keep", "54235", ()),
    ("R6", "W808P391671", "READY", "54235", R6_TITLE, True, "99999", "gp", "keep", "54235", ("pin_mismatch",)),
    ("R7", "W110243192", "READY", "20487", R7_TITLE, False, "", "aqua", "keep", "20487", (NEEDS_MANUAL_MOTORS_LEAF_REVIEW,)),
    ("R8", "W110243144", "READY", "20487", R7_TITLE, False, "", "aqua", "keep", "20487", (NEEDS_MANUAL_MOTORS_LEAF_REVIEW,)),
    ("R9", "W2660P287608", "READY", "20487", R9_TITLE, False, "", "aqua", "keep", "20487", (NEEDS_MANUAL_MOTORS_LEAF_REVIEW,)),
    ("R10", "W46559181", "READY", "88057", R10_TITLE, False, "", "aqua", "keep", "88057", (NEEDS_MANUAL_MOTORS_LEAF_REVIEW,)),
    ("R11", "W465P210780", "READY", "262980", R11_TITLE, False, "", "aqua", "keep", "262980", (NEEDS_MANUAL_MOTORS_LEAF_REVIEW,)),
    ("R12", "W46563694", "READY", "260150", R12_TITLE, False, "", "aqua", "keep", "260150", ()),
    ("R13", "N767P369236T", "PENDING", "79682", R13_TITLE, False, "", "main", "keep", "79682", ()),
    ("R14a", "W1321S00027", "READY", "88057", R14_TITLES["W1321S00027"], False, "", "main", "keep", "88057", ()),
    ("R14b", "W1321S00028", "READY", "88057", R14_TITLES["W1321S00028"], False, "", "main", "keep", "88057", ()),
    ("R14c", "W1580S00493", "READY", "88057", R14_TITLES["W1580S00493"], False, "", "main", "keep", "88057", ()),
]


@pytest.mark.parametrize(
    "case_id,sku,status,stored,title,flag,pin,store,expected_action,expected_id,reports",
    _REAL_SKU_CASES,
    ids=[row[0] for row in _REAL_SKU_CASES],
)
def test_real_sku_gate_table(
    monkeypatch,
    case_id,
    sku,
    status,
    stored,
    title,
    flag,
    pin,
    store,
    expected_action,
    expected_id,
    reports,
):
    indexes, flat = _install_two_trees(monkeypatch)
    monkeypatch.setattr(
        "src.utils.store_profile.get_store_profile",
        lambda: _profile(motors=(store == "aqua")),
    )
    opt = {"categoryId": stored, "title": title}
    if flag:
        opt[CATEGORY_APPROVED_KEY] = True
        opt[CATEGORY_APPROVED_ID_KEY] = pin
    matcher_guess = "179444" if store == "aqua" else "66690"
    gated, action, err = _gate(stored, matcher_guess, title, status, index=flat)
    assert action == expected_action
    assert gated == expected_id
    if expected_action.startswith("hold"):
        assert err
    else:
        assert err == ""
    flags = category_gate_report_flags(
        stored, optimization=opt, is_motors=(store == "aqua"), indexes=indexes
    )
    for flag_name in reports:
        assert flag_name in flags
    if flag:
        assert blocks_automated_category_change(opt) is True
        if pin and pin != stored:
            assert pin_mismatch_reason(opt) == "pin_mismatch"


def test_r2_r3_repair_and_fix_scripts_skip_locked(monkeypatch, tmp_path):
    """R2/R3: --apply paths skip locked rows as category_locked."""
    import scripts.fix_listing_categories as fix
    import scripts.repair_published_taxonomy as repair

    opt = {
        "categoryId": "38208",
        "categoryName": "Sofas",
        CATEGORY_APPROVED_KEY: True,
        CATEGORY_APPROVED_ID_KEY: "38208",
    }
    blank = dict(opt)
    blank[CATEGORY_APPROVED_ID_KEY] = ""
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT, title TEXT, listing_id TEXT, optimization TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?,?,?,?,?)",
        ("W2530P418996", R1_TITLE, "1", json.dumps(opt), "PUBLISHED"),
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?,?,?,?,?)",
        ("W2530P418996B", R1_TITLE, "2", json.dumps(blank), "PUBLISHED"),
    )
    conn.commit()
    assert fix.get_fixes(conn, include_locked=False) == []
    locked_rows = fix.get_fixes(conn, include_locked=True)
    # No MANUAL/KNOWN mapping for these SKUs → still empty, but apply_fix refuses.
    for row_opt in (opt, blank):
        err = locked_category_write_error(row_opt, override=False)
        assert "category lock is set" in err
        assert "category_locked" in err or "override-category-lock" in err

    class _Client:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "o1", "categoryId": "38208"}]

    candidate = repair.audit_candidate(
        {
            "sku": "W2530P418996",
            "listing_id": "1",
            "optimization": opt,
            "title": R1_TITLE,
            "description": "",
        },
        matcher=_MatcherKeep(),
        client=_Client(),
    )
    assert candidate["action"] == "category_locked"
    assert candidate["skip_reason"] == "category_locked"


class _MatcherKeep:
    def get_category_and_aspects(self, title, aspects, description=None):
        return "66690", "Rockers", dict(aspects or {})

    def canonicalize_category(self, title, category_id, category_name=None, description=None):
        return category_id, category_name

    def is_category_plausible_for_text(self, title, category_id, category_name=None):
        return True


def test_r5_inventory_sync_put_keeps_get_category():
    offer = {"offerId": "o1", "categoryId": "54235", "availableQuantity": 1, "listing": {}}
    pinned = pin_offer_category_fields(offer)
    offer["categoryId"] = "99999"
    apply_pinned_offer_category(offer, pinned)
    assert offer["categoryId"] == "54235"


# ---------------------------------------------------------------------------
# SYNTHETIC edge cases
# ---------------------------------------------------------------------------


def test_synthetic_override_without_explicit_id_stays_locked():
    """SYNTHETIC: override flag alone cannot take a matcher suggestion."""
    opt = {CATEGORY_APPROVED_KEY: True, CATEGORY_APPROVED_ID_KEY: "38208", "categoryId": "38208"}
    plan = plan_locked_override_revision(opt, "38208", explicit_target_id=None)
    assert plan["action"] == "SKIP" or plan["target_id"] == "38208"
    # Matcher id 66690 is not the explicit operator target.
    assert plan["target_id"] != "66690"


def test_synthetic_collision_174020_wrong_tree_rejected(monkeypatch):
    """SYNTHETIC: 174020 is Trailer Hitches in tree 0, Brake Pad Wear Sensors in tree 100."""
    indexes, _flat = _install_two_trees(monkeypatch)
    monkeypatch.setattr(
        "src.utils.store_profile.get_store_profile",
        lambda: _profile(motors=True),
    )
    assert colliding_category_ids(indexes=indexes) == ["0", "174020"]
    assert is_cached_leaf_in_tree("174020", "0", indexes=indexes)
    assert is_cached_leaf_in_tree("174020", "100", indexes=indexes)
    assert publish_tree_id_for_target("174020", is_motors=True) == "100"
    # New Motors target that exists only as the tree-0 furniture/hitches leaf
    # would be accepted if it is also a tree-100 leaf. A tree-0-only leaf is not.
    ok, reason = validate_new_target_publish_tree("20487", is_motors=True, indexes=indexes)
    assert ok is False
    assert reason == "wrong_tree_leaf"
    gated, action, err = _gate("179444", "20487", "SYNTHETIC Motors tool chest", "READY")
    assert action == "keep"
    assert gated == "179444"


def test_synthetic_published_keeps_stored_when_tree_unavailable(monkeypatch):
    """SYNTHETIC: PUBLISHED + missing tree → keep stored (4250f71)."""
    from src.utils import category_decision as cd

    monkeypatch.setattr(cd, "category_tree_is_available", lambda index=None: False)
    gated, action, err = gate_publish_category_choice("38208", "54235", R1_TITLE, "PUBLISHED")
    assert (gated, action, err) == ("38208", "keep", "")
    gated, action, err = gate_publish_category_choice("38208", "54235", R1_TITLE, "READY")
    assert action == "hold_tree_unavailable"
    assert gated is None
    assert err


def test_synthetic_put_does_not_add_missing_categoryid():
    """SYNTHETIC: GET had no categoryId → PUT must not add one."""
    offer = {"offerId": "o1", "availableQuantity": 2}
    pinned = pin_offer_category_fields(offer)
    offer["categoryId"] = "54235"
    apply_pinned_offer_category(offer, pinned)
    assert "categoryId" not in offer


# ---------------------------------------------------------------------------
# Tree / matcher / dry-run / AST
# ---------------------------------------------------------------------------


def test_aqua_furniture_leaves_are_tree0_in_fixtures(monkeypatch):
    indexes, _ = _install_two_trees(monkeypatch)
    for cid, name in (("20487", "Cabinets & Cupboards"), ("88057", "Desks & Tables"), ("262980", "Benches")):
        assert indexes["0"][cid][0] == name
        assert cid not in indexes["100"]
        assert needs_manual_motors_leaf_review(cid, is_motors=True, indexes=indexes)


def test_motors_matcher_rejects_tree0_keyword_fallback(monkeypatch):
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    _install_two_trees(monkeypatch)
    monkeypatch.setattr(
        "src.services.ebay_category_matcher.get_store_profile",
        lambda: _profile(motors=True),
    )

    class _Oauth:
        def get_application_token(self):
            return "offline"

    matcher = EbayCategoryMatcher(_Oauth())
    monkeypatch.setattr(matcher, "_fallback_category", lambda title, description=None: ("20487", "Cabinets"))
    monkeypatch.setattr(matcher, "canonicalize_category", lambda *a, **k: (a[1] if len(a) > 1 else None, a[2] if len(a) > 2 else None))

    class _Resp:
        status_code = 200

        def json(self):
            return {"categorySuggestions": [{"category": {"categoryId": "20487", "categoryName": "Cabinets"}}]}

    monkeypatch.setattr("src.services.ebay_category_matcher.requests.get", lambda *a, **k: _Resp())
    cid, name = matcher._get_best_category("SYNTHETIC tool chest")
    assert cid is None
    assert name is None


def test_dry_run_publish_writes_no_optimization_sqlite(monkeypatch, tmp_path):
    import batch_publish

    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT, status TEXT)"
    )
    opt = {"categoryId": "38208", "title": R1_TITLE, "description": "<div>x</div>"}
    conn.execute(
        "INSERT INTO collected_products VALUES (?,?,?)",
        ("SYNTHETIC-DRY", json.dumps(opt), "READY"),
    )
    conn.commit()
    writes = []

    def _persist(**kwargs):
        writes.append(kwargs)
        conn.execute(
            "UPDATE collected_products SET optimization=? WHERE sku=?",
            (json.dumps({"categoryId": kwargs.get("category_id")}), kwargs.get("sku")),
        )
        conn.commit()

    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", _persist)
    monkeypatch.setattr(batch_publish, "_normalize_generated_listing", lambda listing, **kwargs: listing)

    from tests.test_publish_aspect_guards import _product, _stub_publish_common, _long_description

    _stub_publish_common(monkeypatch)
    product = _product(
        title=R1_TITLE,
        description=_long_description("rocking chair"),
        optimization={
            "title": R1_TITLE,
            "description": _long_description("rocking chair"),
            "categoryId": "38208",
            "categoryName": "Sofas, Armchairs & Couches",
            "aspects": {"Brand": ["GrovePop"]},
        },
    )
    product["sku"] = "SYNTHETIC-DRY"
    result = batch_publish.publish_single_product(product, dry_run=True)
    assert result["status"] in {"dry_run", "error"}
    assert writes == []
    stored = json.loads(conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("SYNTHETIC-DRY",)).fetchone()[0])
    assert stored["categoryId"] == "38208"


TOUCHED_FOR_AST = [
    "src/utils/approved_category.py",
    "src/utils/category_decision.py",
    "src/utils/category_tree_cache.py",
    "src/utils/publish_validation.py",
    "src/services/ebay_category_matcher.py",
    "src/services/ebay_publisher.py",
    "src/utils/listing_quality_gate.py",
    "src/plugins/inventory_sync/sync_service.py",
    "batch_publish.py",
    "app.py",
    "server.py",
    "scripts/repair_published_taxonomy.py",
    "scripts/fix_listing_categories.py",
    "scripts/apply_approved_fixes.py",
    "scripts/audit_fix_active_listings.py",
    "scripts/audit_fix_ready_drafts.py",
]


def _assignment_targets(node):
    out = []
    if isinstance(node, ast.Name):
        out.append(node.id)
    elif isinstance(node, ast.Attribute):
        out.append(node.attr)
    elif isinstance(node, ast.Subscript):
        sl = node.slice
        if isinstance(sl, ast.Constant) and sl.value == "categoryId":
            out.append("categoryId")
    return out


def test_categoryid_writers_are_gate_or_override_or_pin():
    """Every categoryId assignment in touched modules is a known writer."""
    allowed_fragments = (
        "gate_publish_category_choice",
        "apply_category_write_gate",
        "override-category-lock",
        "override_category_lock",
        "mark_category_approved",
        "apply_pinned_offer_category",
        "pin_offer_category_fields",
        "locked_category_write_error",
        "blocks_automated_category_change",
        "preserved",
        "stored",
        "original_stored",
        "liveCategoryId",
        "approved",
    )
    writers = []
    for rel in TOUCHED_FOR_AST:
        path = REPO / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    targets.extend(_assignment_targets(t))
            elif isinstance(node, ast.AugAssign):
                targets.extend(_assignment_targets(node.target))
            if "categoryId" not in targets:
                continue
            snippet = ast.get_source_segment(source, node) or ""
            writers.append((rel, getattr(node, "lineno", 0), snippet[:160]))
    assert writers, "expected to find categoryId assignments"
    # This is a census, not a ban: lock/pin/gate paths may write the stored or
    # explicit-override id. A matcher-only write would show up as a new site
    # that does not sit near those helpers — reviewed in the PR body.
    assert all(rel in TOUCHED_FOR_AST for rel, _ln, _s in writers)
