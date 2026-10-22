"""GrovePop category-lock: automation must never move a locked listing."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_fix_active_listings = _load_script("audit_fix_active_listings")
audit_fix_ready_drafts = _load_script("audit_fix_ready_drafts")
repair_published_taxonomy = _load_script("repair_published_taxonomy")

from src.plugins.inventory_sync.sync_service import (
    apply_pinned_offer_category,
    pin_offer_category_fields,
)
from src.utils.approved_category import (
    APPROVED_CATEGORY_INVALID_ID_ERROR,
    CATEGORY_APPROVED_ID_KEY,
    CATEGORY_APPROVED_KEY,
    ApprovedCategoryDecision,
    backfill_approved_category_id,
    blocks_automated_category_change,
    evaluate_approved_category,
    is_category_approved,
    locked_category_write_error,
    mark_category_approved,
    should_auto_remap_invalid_category,
)
from src.utils.listing_quality_gate import (
    ListingProfile,
    normalize_generated_listing,
    validate_listing_quality,
)
from src.utils.publish_validation import category_validation_errors, get_publish_blockers


COOKWARE_LEAF = "20632"
COLLECTIBLES = "975"
GP_COOKWARE_SKU = "GP-CASTIRON-001"


class _RemapMatcher:
    """Forces every stored leaf toward Collectibles (975)."""

    def canonicalize_category(self, *args, **kwargs):
        return COLLECTIBLES, "Collectibles"

    def is_category_plausible_for_text(self, *args, **kwargs):
        return False

    def get_category_and_aspects(self, *args, **kwargs):
        return COLLECTIBLES, "Collectibles", {}

    def _get_category_aspects(self, category_id):
        return [], []

    def _complete_aspects(self, existing_aspects, *args, **kwargs):
        return existing_aspects or {}


def _cookware_opt(*, approved: bool, pinned: str | None = COOKWARE_LEAF, category_id: str = COOKWARE_LEAF) -> dict:
    opt = {
        "title": "Pre-Seasoned Cast Iron Dutch Oven 6 Quart",
        "description": "<html><h2>KEY FEATURES</h2><ul><li>Cast iron</li></ul></html>",
        "categoryId": category_id,
        "categoryName": "Other Cookware",
        "aspects": {"Brand": ["GrovePop"], "Type": ["Dutch Oven"]},
    }
    if approved:
        opt[CATEGORY_APPROVED_KEY] = True
        if pinned is not None:
            opt[CATEGORY_APPROVED_ID_KEY] = pinned
    return opt


def test_operator_marks_approval_on_optimization_json():
    opt = {"categoryId": COOKWARE_LEAF}
    assert is_category_approved(opt) is False
    mark_category_approved(opt)
    assert opt[CATEGORY_APPROVED_KEY] is True
    assert opt[CATEGORY_APPROVED_ID_KEY] == COOKWARE_LEAF
    assert is_category_approved(opt) is True


def test_legacy_flag_does_not_mutate_on_read():
    opt = {"categoryId": COOKWARE_LEAF, CATEGORY_APPROVED_KEY: True}
    assert is_category_approved(opt) is True
    assert CATEGORY_APPROVED_ID_KEY not in opt


def test_legacy_flag_is_honored_only_when_current_id_exists():
    assert is_category_approved({CATEGORY_APPROVED_KEY: True}) is False
    assert is_category_approved({CATEGORY_APPROVED_KEY: True, "categoryId": ""}) is False


def test_pin_mismatch_is_not_approved_but_still_blocks_remap():
    opt = _cookware_opt(approved=True, pinned=COLLECTIBLES, category_id=COOKWARE_LEAF)
    assert is_category_approved(opt) is False
    assert blocks_automated_category_change(opt) is True
    assert locked_category_write_error(opt)


def test_existing_rows_without_new_fields_are_unlocked():
    opt = {"categoryId": COOKWARE_LEAF, "title": "old GP row"}
    assert is_category_approved(opt) is False
    assert blocks_automated_category_change(opt) is False
    assert evaluate_approved_category(COOKWARE_LEAF, opt) == ApprovedCategoryDecision(
        approved=False, preserve=False
    )


def test_blank_lock_fails_closed_for_live_change():
    opt = {CATEGORY_APPROVED_KEY: True, CATEGORY_APPROVED_ID_KEY: "", "categoryId": ""}
    assert blocks_automated_category_change(opt) is True
    decision = evaluate_approved_category("", opt)
    assert decision.approved is False
    assert decision.preserve is False


def test_invalid_lock_id_fails_closed_no_preserve():
    opt = _cookware_opt(approved=True, pinned="not-a-leaf", category_id="not-a-leaf")
    assert blocks_automated_category_change(opt) is True
    decision = evaluate_approved_category("not-a-leaf", opt)
    assert decision.approved is True
    assert decision.preserve is False
    assert decision.error == APPROVED_CATEGORY_INVALID_ID_ERROR


def test_override_clears_the_remap_block():
    opt = _cookware_opt(approved=True)
    assert blocks_automated_category_change(opt, override=True) is False
    assert locked_category_write_error(opt, override=True) == ""


def test_backfill_writes_pin_only_for_legacy_flag():
    opt = {"categoryId": COOKWARE_LEAF, CATEGORY_APPROVED_KEY: True}
    assert backfill_approved_category_id(opt) == COOKWARE_LEAF
    assert opt[CATEGORY_APPROVED_ID_KEY] == COOKWARE_LEAF


def test_normalize_preserves_locked_cookware_leaf():
    opt = _cookware_opt(approved=True)
    out = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        source_description=opt["description"],
        category_matcher=_RemapMatcher(),
    )
    assert out["categoryId"] == COOKWARE_LEAF


def test_normalize_keeps_leaf_when_lock_pin_mismatches():
    opt = _cookware_opt(approved=True, pinned=COLLECTIBLES, category_id=COOKWARE_LEAF)
    out = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        source_description=opt["description"],
        category_matcher=_RemapMatcher(),
    )
    assert out["categoryId"] == COOKWARE_LEAF
    assert out["categoryName"] == "Other Cookware"


def test_normalize_does_not_invent_category_for_blank_lock():
    opt = {
        "title": "Modern Sectional Sofa",
        "description": "<html><h2>KEY FEATURES</h2><ul><li>Sofa</li></ul></html>",
        "categoryId": "",
        "aspects": {"Brand": ["GrovePop"]},
        CATEGORY_APPROVED_KEY: True,
        CATEGORY_APPROVED_ID_KEY: "",
    }
    out = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        source_description=opt["description"],
    )
    assert str(out.get("categoryId") or "") == ""


def test_normalize_unlocked_cookware_may_remap():
    opt = _cookware_opt(approved=False)
    out = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        source_description=opt["description"],
        category_matcher=_RemapMatcher(),
    )
    assert out["categoryId"] == COLLECTIBLES


def test_validate_skips_mismatch_when_locked(monkeypatch):
    monkeypatch.setattr(
        "src.utils.listing_quality_gate.classify_listing_profile",
        lambda *a, **k: ListingProfile(kind="collectible", category_id=COLLECTIBLES, category_name="Collectibles"),
    )
    locked = validate_listing_quality(_cookware_opt(approved=True), category_matcher=_RemapMatcher())
    assert not any(issue.field == "categoryId" for issue in locked)

    unlocked = validate_listing_quality(_cookware_opt(approved=False), category_matcher=_RemapMatcher())
    assert any(issue.code == "category_mismatch" for issue in unlocked)


def test_publish_blockers_skip_canonicalize_when_locked():
    blockers = get_publish_blockers(
        _RemapMatcher(),
        "Pre-Seasoned Cast Iron Dutch Oven 6 Quart",
        {"Item Length": ["12 in"], "Item Width": ["10 in"], "Item Height": ["8 in"]},
        COOKWARE_LEAF,
        "Other Cookware",
        skip_canonicalize=True,
    )
    assert blockers == []

    remapped = category_validation_errors(
        _RemapMatcher(),
        "Pre-Seasoned Cast Iron Dutch Oven 6 Quart",
        COOKWARE_LEAF,
        "Other Cookware",
        skip_canonicalize=False,
    )
    assert remapped  # unlocked path still reports implausible / remapped leaf


def test_audit_does_not_emit_category_fix_for_locked_cookware(monkeypatch):
    audit = audit_fix_active_listings
    monkeypatch.setattr(audit, "get_category_matcher", lambda: _RemapMatcher())
    monkeypatch.setattr(
        audit,
        "classify_listing_profile",
        lambda *a, **k: ListingProfile(kind="collectible", category_id=COLLECTIBLES, category_name="Collectibles"),
    )
    opt = _cookware_opt(approved=True)
    issues, fixes = audit.audit_single_product(
        GP_COOKWARE_SKU,
        opt["title"],
        "{}",
        "{}",
        json.dumps(opt),
        opt["description"],
    )
    assert "categoryId" not in fixes
    assert "categoryName" not in fixes


def test_audit_unlocked_cookware_still_offers_remap(monkeypatch):
    audit = audit_fix_active_listings
    monkeypatch.setattr(audit, "get_category_matcher", lambda: _RemapMatcher())
    monkeypatch.setattr(
        audit,
        "classify_listing_profile",
        lambda *a, **k: ListingProfile(kind="collectible", category_id=COLLECTIBLES, category_name="Collectibles"),
    )
    opt = _cookware_opt(approved=False)
    issues, fixes = audit.audit_single_product(
        GP_COOKWARE_SKU,
        opt["title"],
        "{}",
        "{}",
        json.dumps(opt),
        opt["description"],
    )
    assert fixes.get("categoryId") == COLLECTIBLES


def test_audit_blank_lock_fails_closed_no_category_fix(monkeypatch):
    audit = audit_fix_active_listings
    monkeypatch.setattr(audit, "get_category_matcher", lambda: _RemapMatcher())
    monkeypatch.setattr(
        audit,
        "classify_listing_profile",
        lambda *a, **k: ListingProfile(kind="collectible", category_id=COLLECTIBLES, category_name="Collectibles"),
    )
    opt = _cookware_opt(approved=True, pinned="", category_id="")
    opt["categoryId"] = ""
    issues, fixes = audit.audit_single_product(
        GP_COOKWARE_SKU,
        "Cast Iron Skillet",
        "{}",
        "{}",
        json.dumps(opt),
        opt["description"],
    )
    assert "categoryId" not in fixes


def test_fix_listing_refuses_locked_category_without_override():
    results = audit_fix_active_listings.fix_listing_on_ebay(
        GP_COOKWARE_SKU,
        {
            "sku": GP_COOKWARE_SKU,
            "title": "Cast Iron Dutch Oven",
            "description": "",
            "attributes": "{}",
            "specs": "{}",
            "optimization": json.dumps(_cookware_opt(approved=True)),
        },
        {"categoryId": COLLECTIBLES, "categoryName": "Collectibles"},
        ebay_client=SimpleNamespace(),
        db_conn=None,
        override_category_lock=False,
    )
    assert any("category lock" in str(row) for row in results)
    assert not any(str(row).startswith("ERROR") is False and "category" in str(row).lower() for row in results)


def test_choose_ready_category_keeps_locked_leaf():
    cat_id, cat_name, aspects = audit_fix_ready_drafts.choose_ready_category(
        _RemapMatcher(),
        "Cast Iron Dutch Oven",
        "Cast Iron Dutch Oven",
        "cast iron cookware",
        {"Brand": ["GrovePop"]},
        COOKWARE_LEAF,
        "Other Cookware",
        optimization=_cookware_opt(approved=True),
    )
    assert cat_id == COOKWARE_LEAF
    assert cat_name == "Other Cookware"


def test_choose_ready_category_unlocked_can_remap():
    cat_id, _name, _aspects = audit_fix_ready_drafts.choose_ready_category(
        _RemapMatcher(),
        "Cast Iron Dutch Oven",
        "Cast Iron Dutch Oven",
        "cast iron cookware",
        {"Brand": ["GrovePop"]},
        COOKWARE_LEAF,
        "Other Cookware",
        optimization=_cookware_opt(approved=False),
    )
    assert cat_id == COLLECTIBLES


def test_inventory_sync_put_cannot_change_category():
    live = {
        "offerId": "off-1",
        "categoryId": COOKWARE_LEAF,
        "listing": {"categoryId": COOKWARE_LEAF, "listingStatus": "ACTIVE"},
        "pricingSummary": {"price": {"value": "49.00"}},
        "availableQuantity": 1,
    }
    pinned = pin_offer_category_fields(live)
    live["categoryId"] = COLLECTIBLES
    live["listing"]["categoryId"] = COLLECTIBLES
    live["pricingSummary"]["price"]["value"] = "59.00"
    apply_pinned_offer_category(live, pinned)
    assert live["categoryId"] == COOKWARE_LEAF
    assert live["listing"]["categoryId"] == COOKWARE_LEAF
    assert live["pricingSummary"]["price"]["value"] == "59.00"


def test_repair_taxonomy_marks_locked_action(monkeypatch):
    product_row = {
        "sku": GP_COOKWARE_SKU,
        "listing_id": "123",
        "title": "Cast Iron Dutch Oven",
        "description": "cast iron",
        "optimization": _cookware_opt(approved=True),
        "attributes": {},
        "specs": {},
        "images": [],
    }

    monkeypatch.setattr(
        repair_published_taxonomy,
        "get_live_offer_info",
        lambda _client, _sku: {
            "offer_id": "off-1",
            "live_category_id": COOKWARE_LEAF,
            "live_category_name": "Other Cookware",
        },
    )
    candidate = repair_published_taxonomy.audit_candidate(product_row, _RemapMatcher(), client=None)
    assert candidate["action"] == "LOCKED"


def test_evaluate_preserves_numeric_cookware_leaf_without_main_cache():
    decision = evaluate_approved_category(COOKWARE_LEAF, _cookware_opt(approved=True))
    assert decision.approved is True
    assert decision.preserve is True
    assert decision.error is None


def test_publish_rule_blank_or_mismatched_lock_fails_closed():
    """Publish callers refuse to rematch when the flag is set but cannot preserve."""
    blank = {CATEGORY_APPROVED_KEY: True, "categoryId": ""}
    decision = evaluate_approved_category("", blank)
    assert decision.preserve is False
    assert blocks_automated_category_change(blank) is True

    mismatched = _cookware_opt(approved=True, pinned=COLLECTIBLES, category_id=COOKWARE_LEAF)
    decision = evaluate_approved_category(COOKWARE_LEAF, mismatched)
    assert decision.preserve is False
    assert blocks_automated_category_change(mismatched) is True


def test_normalize_pin_mismatch_does_not_remap():
    """Flag + pin/current mismatch must keep the stored leaf, not follow rematch."""
    opt = _cookware_opt(approved=True, pinned=COLLECTIBLES, category_id=COOKWARE_LEAF)
    out = normalize_generated_listing(
        opt,
        source_title=opt["title"],
        source_description=opt["description"],
        category_matcher=_RemapMatcher(),
    )
    assert out["categoryId"] == COOKWARE_LEAF


def test_json_string_optimization_is_treated_as_locked():
    payload = json.dumps(_cookware_opt(approved=True))
    assert blocks_automated_category_change(payload) is True
    assert is_category_approved(payload) is True
    assert blocks_automated_category_change("{not-json") is False
    assert blocks_automated_category_change(None) is False


def test_stringy_true_flag_blocks_and_legacy_rows_without_fields_do_not():
    stringy = {"categoryId": COOKWARE_LEAF, CATEGORY_APPROVED_KEY: "true"}
    assert blocks_automated_category_change(stringy) is True
    assert is_category_approved(stringy) is True

    yes_flag = {"categoryId": COOKWARE_LEAF, CATEGORY_APPROVED_KEY: "yes"}
    assert blocks_automated_category_change(yes_flag) is True

    legacy_missing = {"categoryId": COOKWARE_LEAF, "title": "pre-lock GP row"}
    assert CATEGORY_APPROVED_KEY not in legacy_missing
    assert CATEGORY_APPROVED_ID_KEY not in legacy_missing
    assert blocks_automated_category_change(legacy_missing) is False
    assert is_category_approved(legacy_missing) is False


def test_publisher_blank_lock_does_not_follow_suggested_id():
    import logging

    from src.services.ebay_publisher import EbayPublisher

    pub = object.__new__(EbayPublisher)
    pub.logger = logging.getLogger("test-category-lock")

    def _must_not_complete(*_a, **_k):
        raise AssertionError("locked blank category must not rematch or complete aspects")

    pub._ensure_required_aspects = _must_not_complete
    product = {
        "optimization": {CATEGORY_APPROVED_KEY: True, "categoryId": ""},
    }
    cat_id, _name, aspects = EbayPublisher._get_category_and_aspects(
        pub,
        "Pre-Seasoned Cast Iron Dutch Oven",
        {"Brand": ["GrovePop"]},
        product,
        suggested_id=COLLECTIBLES,
    )
    assert cat_id == ""
    assert aspects == {"Brand": ["GrovePop"]}


def test_publisher_json_string_optimization_keeps_locked_leaf():
    import logging

    from src.services.ebay_publisher import EbayPublisher

    pub = object.__new__(EbayPublisher)
    pub.logger = logging.getLogger("test-category-lock")
    pub._ensure_required_aspects = lambda *_a, **_k: {"Brand": ["GrovePop"]}
    product = {"optimization": json.dumps(_cookware_opt(approved=True))}
    cat_id, _name, _aspects = EbayPublisher._get_category_and_aspects(
        pub,
        "Pre-Seasoned Cast Iron Dutch Oven",
        {"Brand": ["GrovePop"]},
        product,
        suggested_id=COLLECTIBLES,
    )
    assert cat_id == COOKWARE_LEAF


def test_inventory_pin_missing_category_does_not_invent_a_leaf():
    live = {
        "offerId": "off-2",
        "listing": {"listingStatus": "ACTIVE"},
        "pricingSummary": {"price": {"value": "49.00"}},
        "availableQuantity": 0,
    }
    pinned = pin_offer_category_fields(live)
    live["categoryId"] = COLLECTIBLES
    live["listing"]["categoryId"] = COLLECTIBLES
    apply_pinned_offer_category(live, pinned)
    assert "categoryId" not in live
    assert "categoryId" not in live["listing"]


def test_publish_retry_does_not_rematch_locked_cookware_leaf():
    """GP 975->20632: invalid-category retry must not write a new leaf."""
    locked = _cookware_opt(approved=True)
    assert should_auto_remap_invalid_category(locked, preserve=True) is False
    assert should_auto_remap_invalid_category(locked, preserve=False) is False

    mismatched = _cookware_opt(approved=True, pinned=COLLECTIBLES, category_id=COOKWARE_LEAF)
    assert should_auto_remap_invalid_category(mismatched, preserve=False) is False

    blank = {CATEGORY_APPROVED_KEY: True, "categoryId": ""}
    assert should_auto_remap_invalid_category(blank, preserve=False) is False

    unlocked = _cookware_opt(approved=False)
    assert should_auto_remap_invalid_category(unlocked, preserve=False) is True


def test_locked_live_readback_must_not_overwrite_category_id():
    """Successful publish must not persist a different live leaf over the lock."""
    opt = _cookware_opt(approved=True)
    live_category_id = COLLECTIBLES
    if not blocks_automated_category_change(opt):
        opt["categoryId"] = live_category_id
    assert opt["categoryId"] == COOKWARE_LEAF
    opt["liveCategoryId"] = live_category_id
    assert opt["categoryId"] == COOKWARE_LEAF
