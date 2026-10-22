"""AquaRides category lock: tree 100, dry-run persistence, and fail-closed pins.

No live eBay calls. Motors behavior is forced through the store profile.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import batch_publish
from scripts.audit_fix_active_listings import build_live_listing_opt_snapshot
from scripts.repair_published_taxonomy import audit_candidate
from src.db.collection_models import Base, CollectedProduct
from src.plugins.inventory_sync.sync_service import (
    apply_pinned_offer_category,
    pin_offer_category_fields,
)
from src.utils.approved_category import (
    apply_live_category_readback,
    blocks_automated_category_change,
    evaluate_approved_category,
    is_category_approved,
)
from src.utils.listing_quality_gate import normalize_generated_listing
from src.utils.motors_category_tree import is_tree100_leaf
from src.utils.store_profile import get_store_profile

ROOT = Path(__file__).resolve().parent.parent
SKU = "W2660P287608"
APPROVED = "179444"
TREE0_CABINET = "20487"
OTHER_AUTO_TOOLS = "43998"
DESCRIPTION = (
    "<div><h3>KEY FEATURES</h3><ul>"
    "<li>Five drawers keep sockets and hand tools sorted in a rolling cabinet.</li>"
    "<li>Locking casters move the chest around a garage without dragging it.</li>"
    "<li>Steel housing stands up to shop storage cabinet use.</li>"
    "</ul></div>"
)
TITLE = "5-Drawer Rolling Tool Cabinet Storage Cabinet Garage Shop"


def _motors_profile():
    base = get_store_profile()
    return replace(
        base,
        category_tree_id="100",
        listing_channel="trading",
        ebay_site_id="100",
        store_kind="auto",
    )


def use_motors(monkeypatch):
    profile = _motors_profile()

    def _profile():
        return profile

    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _profile)
    monkeypatch.setattr("batch_publish.get_store_profile", _profile)
    monkeypatch.setattr("src.services.ebay_category_matcher.get_store_profile", _profile)
    monkeypatch.setattr("src.services.ebay_publisher.get_store_profile", _profile)
    return profile


def _optimization(**extra):
    opt = {
        "title": TITLE,
        "description": DESCRIPTION,
        "categoryId": APPROVED,
        "categoryName": "Garage/Shop Tool Chests",
        "aspects": {
            "Brand": ["AquaRides"],
            "Type": ["Tool Chest"],
            "Item Length": ["26 in"],
            "Item Width": ["18 in"],
            "Item Height": ["32 in"],
        },
    }
    opt.update(extra)
    return opt


def _locked(**extra):
    opt = _optimization(
        categoryApproved=True,
        categoryApprovedId=APPROVED,
    )
    opt.update(extra)
    return opt


def _product(opt):
    return {
        "sku": SKU,
        "title": TITLE,
        "description": DESCRIPTION,
        "price": 180.0,
        "suggested_price": 249.0,
        "shipping": 0,
        "images": ["https://example.com/cabinet.jpg"],
        "videos": [],
        "attributes": {
            "Assembled Length (in.)": "26",
            "Assembled Width (in.)": "18",
            "Assembled Height (in.)": "32",
            "Product Weight (lbs.)": "74",
        },
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 120},
        "optimization": opt,
    }


class _PantryFallbackMatcher:
    """Matcher that first offers 43998, then canonicalizes everything to 20487."""

    def __init__(self, oauth):
        self.oauth = oauth

    def canonicalize_category(self, title_context, category_id, category_name, description=None):
        return TREE0_CABINET, "Cabinets & Cupboards"

    def get_category_and_aspects(self, title, aspects, description=None):
        return OTHER_AUTO_TOOLS, "Other Auto Tools & Supplies", dict(aspects or {})

    def is_category_plausible_for_text(self, title, category_id, category_name):
        return True

    def _get_category_aspects(self, category_id):
        return [], []

    def _complete_aspects(self, existing, required, recommended, title, category_name):
        return dict(existing or {})


class _OAuth:
    api_base = "https://api.ebay.test"

    def __init__(self, environment):
        self.environment = environment

    def is_authorized(self):
        return True

    def get_application_token(self):
        return "app-token"

    def get_valid_token(self):
        return "token"


class _Policy:
    def __init__(self, oauth):
        self.oauth = oauth


class _Client:
    calls = []

    def __init__(self, oauth, policy_manager):
        self.oauth = oauth

    def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
        return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

    def add_fixed_price_item_motors(self, product, category_id=None, price=None, quantity=None):
        _Client.calls.append(str(category_id))
        if getattr(self, "fail_once", False) and _Client.calls.count(str(category_id)) == 1:
            raise RuntimeError("Invalid category")
        return {"itemId": "999000111", "ack": "Success"}


@pytest.fixture
def motors(monkeypatch):
    use_motors(monkeypatch)
    monkeypatch.setattr(batch_publish, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(batch_publish, "calculate_smart_final_price", lambda product, market_p: 249.0)
    monkeypatch.setattr(batch_publish, "_is_sellable_leaf_category", lambda oauth, category_id: True)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr(batch_publish.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        batch_publish,
        "run_listing_qc",
        lambda **kwargs: {
            "status": "pass",
            "blockers": [],
            "warnings": [],
            "source_fingerprint": "source",
            "candidate_fingerprint": "candidate",
            "ruleset_version": "listing-qc-v2",
            "fact_sheet_version": 1,
            "rule_ids": [],
            "experience_ids": [],
            "evidence_refs": [],
        },
    )
    real_connect = batch_publish.sqlite3.connect

    def _connect(path, *args, **kwargs):
        if str(path).endswith("ebay_collection.db"):
            return real_connect(":memory:")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(batch_publish.sqlite3, "connect", _connect)
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _OAuth)
    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", _PantryFallbackMatcher)
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", _Policy)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _Client)
    _Client.calls = []
    return monkeypatch


def _temp_session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'lock.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("src.db.collection_db.SessionLocal", Session)
    return engine, Session


def _insert(session_cls, opt):
    db = session_cls()
    db.add(
        CollectedProduct(
            sku=SKU,
            title=TITLE,
            price=180.0,
            description=DESCRIPTION,
            images=["https://example.com/cabinet.jpg"],
            optimization=opt,
            logs=["seed"],
            status="READY",
        )
    )
    db.commit()
    db.close()


def _raw_optimization(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT optimization FROM collected_products WHERE sku = :sku"),
            {"sku": SKU},
        ).scalar()


def test_tree_snapshot_contains_approved_leaf_and_rejects_cabinets():
    assert is_tree100_leaf(APPROVED)
    assert is_tree100_leaf(OTHER_AUTO_TOOLS)
    assert is_tree100_leaf("174020")
    assert not is_tree100_leaf(TREE0_CABINET)


def test_locked_179444_survives_normalize_against_pantry_fallback(motors):
    class _Matcher:
        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return TREE0_CABINET, "Cabinets & Cupboards"

    normalized = normalize_generated_listing(
        _locked(),
        source_title=TITLE,
        source_description=DESCRIPTION,
        category_matcher=_Matcher(),
    )
    assert normalized["categoryId"] == APPROVED
    assert normalized["categoryApprovedId"] == APPROVED


def test_locked_dry_run_keeps_optimization_bytes_and_category(motors, tmp_path, monkeypatch):
    engine, Session = _temp_session(tmp_path, monkeypatch)
    opt = _locked()
    _insert(Session, opt)
    before = _raw_optimization(engine)

    result = batch_publish.publish_single_product(_product(opt), dry_run=True)

    assert result["status"] == "dry_run"
    assert result["category_id"] == APPROVED
    assert _Client.calls == []
    assert _raw_optimization(engine) == before
    stored = json.loads(_raw_optimization(engine))
    assert stored["categoryId"] == APPROVED
    assert stored["categoryApprovedId"] == APPROVED


def test_locked_publish_and_invalid_category_retry_stay_on_179444(motors, tmp_path, monkeypatch):
    engine, Session = _temp_session(tmp_path, monkeypatch)
    opt = _locked()
    _insert(Session, opt)
    _Client.fail_once = True
    try:
        result = batch_publish.publish_single_product(_product(opt), dry_run=False)
    finally:
        _Client.fail_once = False

    assert result["status"] == "success"
    assert _Client.calls == [APPROVED, APPROVED]
    stored = json.loads(_raw_optimization(engine))
    assert stored["categoryId"] == APPROVED
    assert stored["categoryApprovedId"] == APPROVED


def test_unlocked_stored_tree100_leaf_is_not_replaced_by_rejected_canonicalize(motors, tmp_path, monkeypatch):
    engine, Session = _temp_session(tmp_path, monkeypatch)
    opt = _optimization()
    _insert(Session, opt)
    before = _raw_optimization(engine)

    result = batch_publish.publish_single_product(_product(opt), dry_run=True)

    assert result["status"] == "dry_run"
    assert result["category_id"] == APPROVED
    assert _Client.calls == []
    assert _raw_optimization(engine) == before


def test_unlocked_motors_matcher_20487_is_blocked_not_published(motors, tmp_path, monkeypatch):
    engine, Session = _temp_session(tmp_path, monkeypatch)
    opt = _optimization(categoryId=TREE0_CABINET, categoryName="Cabinets & Cupboards")
    opt.pop("categoryApproved", None)
    opt.pop("categoryApprovedId", None)
    _insert(Session, opt)
    before = _raw_optimization(engine)

    result = batch_publish.publish_single_product(_product(opt), dry_run=False)

    assert result["status"] == "error"
    assert "not a leaf in eBay Motors category tree 100" in result["message"]
    assert _Client.calls == []
    assert _raw_optimization(engine) == before


def test_blank_pin_and_mismatched_pin_fail_closed(motors, tmp_path, monkeypatch):
    engine, Session = _temp_session(tmp_path, monkeypatch)

    blank = _optimization(categoryId="abc", categoryName="", categoryApproved=True)
    blank.pop("categoryApprovedId", None)
    _insert(Session, blank)
    before_blank = _raw_optimization(engine)
    blank_result = batch_publish.publish_single_product(_product(blank), dry_run=True)
    assert blank_result["status"] == "error"
    assert "invalid_or_blank_category_id" in blank_result["message"]
    assert _Client.calls == []
    assert _raw_optimization(engine) == before_blank

    mismatched = _locked(categoryApprovedId=OTHER_AUTO_TOOLS)
    db = Session()
    row = db.query(CollectedProduct).filter_by(sku=SKU).one()
    row.optimization = mismatched
    db.commit()
    db.close()
    before_mismatch = _raw_optimization(engine)
    mismatch_result = batch_publish.publish_single_product(_product(mismatched), dry_run=True)
    assert mismatch_result["status"] == "error"
    assert _Client.calls == []
    assert _raw_optimization(engine) == before_mismatch
    assert json.loads(before_mismatch)["categoryId"] == APPROVED


def test_legacy_flag_without_pin_honors_current_leaf_and_missing_keys_stay_unlocked(motors):
    legacy = _optimization(categoryApproved=True)
    assert "categoryApprovedId" not in legacy
    decision = evaluate_approved_category(APPROVED, legacy)
    assert decision.preserve is True
    assert is_category_approved(legacy, APPROVED) is True

    unlocked = _optimization()
    assert blocks_automated_category_change(unlocked) is False
    assert evaluate_approved_category(APPROVED, unlocked).preserve is False

    as_text = json.dumps(_locked())
    assert is_category_approved(as_text, APPROVED) is True
    assert blocks_automated_category_change(as_text) is True


def test_json_string_lock_survives_dry_run(motors, tmp_path, monkeypatch):
    engine, Session = _temp_session(tmp_path, monkeypatch)
    opt = _locked()
    _insert(Session, opt)
    before = _raw_optimization(engine)
    result = batch_publish.publish_single_product(_product(json.dumps(opt)), dry_run=True)
    assert result["status"] == "dry_run"
    assert result["category_id"] == APPROVED
    assert _raw_optimization(engine) == before


def test_live_readback_does_not_overwrite_locked_category():
    locked = _locked()
    apply_live_category_readback(locked, TREE0_CABINET, "Cabinets & Cupboards")
    assert locked["categoryId"] == APPROVED
    assert locked["categoryApprovedId"] == APPROVED
    assert locked["liveCategoryId"] == TREE0_CABINET

    snapshot = build_live_listing_opt_snapshot(
        _locked(),
        inventory_item={"product": {"title": TITLE, "description": DESCRIPTION, "aspects": {}}},
        offer={"categoryId": TREE0_CABINET, "category": {"categoryName": "Cabinets & Cupboards"}},
    )
    assert snapshot["categoryId"] == APPROVED
    assert snapshot["liveCategoryId"] == TREE0_CABINET
    assert snapshot["categoryName"] == "Garage/Shop Tool Chests"

    unlocked = _optimization()
    apply_live_category_readback(unlocked, OTHER_AUTO_TOOLS, "Other Auto Tools & Supplies")
    assert unlocked["categoryId"] == OTHER_AUTO_TOOLS


def test_high_fee_blacklist_blocks_locked_leaf(motors, tmp_path, monkeypatch):
    engine, Session = _temp_session(tmp_path, monkeypatch)
    opt = _locked()
    _insert(Session, opt)
    before = _raw_optimization(engine)
    blacklist = tmp_path / "fees.json"
    blacklist.write_text(
        json.dumps({"publish_skip_category_ids": [APPROVED]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", str(blacklist))

    result = batch_publish.publish_single_product(_product(opt), dry_run=False)

    assert result["status"] == "error"
    assert "high_insertion_fee" in result["message"]
    assert _Client.calls == []
    assert _raw_optimization(engine) == before


def test_override_never_follows_matcher_and_keyword_collision_is_blocked(motors, monkeypatch):
    row = {
        "sku": SKU,
        "title": TITLE,
        "description": DESCRIPTION,
        "listing_id": "1",
        "optimization": _locked(),
    }

    class _Matcher:
        def get_category_and_aspects(self, title, aspects, description=""):
            return TREE0_CABINET, "Cabinets & Cupboards", dict(aspects or {})

        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

    class _Offers:
        def get_offers_by_sku(self, sku):
            return [{
                "offerId": "offer-1",
                "categoryId": TREE0_CABINET,
                "category": {"categoryId": TREE0_CABINET, "categoryName": "Cabinets & Cupboards"},
            }]

    locked = audit_candidate(row, _Matcher(), _Offers())
    assert locked["action"] == "LOCKED"
    assert locked["target_category_id"] == APPROVED

    restored = audit_candidate(
        row,
        _Matcher(),
        _Offers(),
        override_category_lock=True,
    )
    assert restored["action"] == "REVISE_LIVE"
    assert restored["target_category_id"] == APPROVED

    explicit = audit_candidate(
        row,
        _Matcher(),
        _Offers(),
        override_category_lock=True,
        explicit_targets={SKU: OTHER_AUTO_TOOLS},
    )
    assert explicit["action"] == "REVISE_LIVE"
    assert explicit["target_category_id"] == OTHER_AUTO_TOOLS

    bad_explicit = audit_candidate(
        row,
        _Matcher(),
        _Offers(),
        override_category_lock=True,
        explicit_targets={SKU: TREE0_CABINET},
    )
    assert bad_explicit["action"] == "SKIP"
    assert bad_explicit["target_category_id"] != TREE0_CABINET

    from src.services.ebay_publisher import EbayPublisher

    class _NoMatch:
        def __init__(self, oauth):
            pass

        def get_category_and_aspects(self, title, aspects, description=None):
            return None, None, aspects

    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", _NoMatch)
    publisher = EbayPublisher.__new__(EbayPublisher)
    publisher.oauth = _OAuth("PRODUCTION")
    publisher.logger = batch_publish.logger
    category_id, _, _ = publisher._get_category_and_aspects(
        'Class 3 Trailer Hitch Receiver 2"',
        {},
        {"optimization": {}},
    )
    assert category_id == ""
    assert category_id != "174020"


def _load_apply_approved():
    spec = importlib.util.spec_from_file_location(
        "apply_approved_fixes_lock",
        ROOT / "scripts" / "apply_approved_fixes.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mark_category_approved_requires_explicit_matching_leaf(tmp_path, monkeypatch):
    use_motors(monkeypatch)
    module = _load_apply_approved()
    db_path = tmp_path / "stamp.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (SKU, json.dumps(_optimization())),
    )
    conn.commit()
    conn.close()

    missing = module.process_rows(
        [{"sku": SKU, "fix_keys": ["categoryApproved"], "note": ""}],
        apply=True,
        mark_category_approved=True,
        stop_on_error=True,
        db_path=db_path,
        run_audit=lambda *args, **kwargs: None,
    )
    assert missing == 1

    refused = module.stamp_local_category_approved(
        SKU,
        approved_category_id=TREE0_CABINET,
        apply=True,
        db_path=db_path,
    )
    assert refused["ok"] is False

    stamped = module.stamp_local_category_approved(
        SKU,
        approved_category_id=APPROVED,
        apply=True,
        db_path=db_path,
    )
    assert stamped["ok"] is True
    conn = sqlite3.connect(db_path)
    stored = json.loads(conn.execute("SELECT optimization FROM collected_products").fetchone()[0])
    conn.close()
    assert stored["categoryId"] == APPROVED
    assert stored["categoryApproved"] is True
    assert stored["categoryApprovedId"] == APPROVED

    locked_row = {
        "sku": SKU,
        "fix_keys": ["categoryId"],
        "category_id": OTHER_AUTO_TOOLS,
        "note": "",
    }
    blocked = module.process_rows(
        [locked_row],
        apply=False,
        mark_category_approved=False,
        stop_on_error=True,
        db_path=db_path,
        override_category_lock=False,
        run_audit=lambda *args, **kwargs: pytest.fail("locked remap must not call audit"),
    )
    assert blocked == 1


def test_inventory_sync_put_restores_offer_category():
    offer = {
        "offerId": "offer-1",
        "categoryId": APPROVED,
        "availableQuantity": 0,
        "listing": {"listingId": "1", "categoryId": APPROVED},
        "pricingSummary": {"price": {"value": "10.00"}},
    }
    pinned = pin_offer_category_fields(offer)
    offer["availableQuantity"] = 1
    offer["categoryId"] = TREE0_CABINET
    offer["listing"]["categoryId"] = TREE0_CABINET
    offer["pricingSummary"]["price"]["value"] = "12.00"
    apply_pinned_offer_category(offer, pinned)
    assert offer["categoryId"] == APPROVED
    assert offer["listing"]["categoryId"] == APPROVED
    assert offer["availableQuantity"] == 1
    assert offer["pricingSummary"]["price"]["value"] == "12.00"
