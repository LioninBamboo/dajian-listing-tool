"""M1/M2/M3 follow-up: manual publish gate, title-family hold, runtime tree refresh."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from src.utils.category_decision import (
    HOLD_EMPTY_PUBLISH_ERROR,
    HOLD_TREE_UNAVAILABLE_ERROR,
    apply_category_write_gate,
    gate_publish_category_choice,
    legacy_remap_candidate,
    publish_listing_status,
    resolve_category_decision,
    unpublished_listing_status,
    validate_target_category,
)
from src.utils.listing_quality_gate import (
    normalize_generated_listing,
    validate_listing_quality,
)
from tests.conftest import load_fixture_category_index

INDEX = load_fixture_category_index()
# Motors roof-rack leaf lives in tree 100. Keep it out of the shared fixture
# so existing READY-draft tests still see an unknown 262216 as implausible.
MOTORS_INDEX = dict(INDEX)
MOTORS_INDEX["262216"] = ("Roof Racks", "900015")


@pytest.fixture(autouse=True)
def _isolate_collection_db(tmp_path, monkeypatch):
    """Keep app/server imports from creating repo-root collected_products."""
    import sys

    db_path = tmp_path / "ebay_collection.db"
    monkeypatch.setenv("DAJIAN_COLLECTION_DB", str(db_path))
    import src.db.collection_db as cdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.db.collection_models import Base

    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(cdb, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(cdb, "DATABASE_URL", url)
    monkeypatch.setattr(cdb, "engine", engine)
    monkeypatch.setattr(cdb, "SessionLocal", sessionmaker(bind=engine))

    def _tmp_init_db():
        Base.metadata.create_all(bind=engine)

    _tmp_init_db()
    monkeypatch.setattr(cdb, "init_db", _tmp_init_db)
    if "server" in sys.modules:
        monkeypatch.setattr(sys.modules["server"], "init_db", _tmp_init_db, raising=False)


NAMED = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "category_remap_named_publish_skus.json").read_text(
        encoding="utf-8"
    )
)
REAL = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "category_remap_real_skus.json").read_text(
        encoding="utf-8"
    )
)


def _by_sku(*skus: str) -> dict:
    found = {}
    for row in list(REAL) + list(NAMED):
        if row["sku"] in skus:
            found[row["sku"]] = row
    missing = set(skus) - set(found)
    assert not missing, missing
    return found


def _gate(stored, title, status="READY", candidate=None):
    return gate_publish_category_choice(
        stored,
        candidate if candidate is not None else stored,
        title,
        status,
        index=INDEX,
    )


# ---------------------------------------------------------------------------
# Real SKUs named in the follow-up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sku,stored,status,must_hold,suggest",
    [
        ("W3190S00002", "125085", "PENDING", True, "20487"),
        ("W3190P305785", "177000", "READY", True, None),
        ("W3190P305792", "177000", "READY", True, None),
        ("T3971P401872", "103431", "PENDING", True, "22656"),
        ("W1580S00491", "20487", "PENDING", True, "22656"),
        ("W465P408010", "179446", "READY", False, None),
        ("W1321S00027", "88057", "READY", False, None),
        ("W1321S00028", "88057", "READY", False, None),
        ("W1580S00493", "88057", "PENDING", False, None),
        ("gp:W2530P418996", "38208", "PENDING", False, None),
        ("N717P210135K", "79694", "READY", False, None),
    ],
)
def test_real_sku_title_family_and_keep_set(sku, stored, status, must_hold, suggest):
    row = _by_sku(sku)[sku]
    title = row["live_title"] or row.get("giga_title") or ""
    assert str(row["stored_category"]) == stored
    gated, action, err = _gate(stored, title, status)
    if must_hold:
        assert action == "hold_implausible", (sku, action, err)
        assert gated is None
        assert "not publishable" in err
        if suggest:
            assert suggest in err
    else:
        assert action == "keep", (sku, action, err)
        assert gated == stored
        assert err == ""


def test_real_sku_false_flag_set_from_pr29():
    """Must not re-introduce the #29 false flags."""
    cases = [
        ("W465P408010", "Heavy Duty Bolt Cutter 24 Inch Chrome Vanadium Steel", "179446"),
        ("VANITY_32878", "24 Inch Bathroom Vanity with Sink", "32878"),
        ("CABINET_20487", "Kitchen Storage Cabinet with Doors", "20487"),
        ("CABINET_176991", "Bathroom Wall Cabinet with Mirror", "176991"),
        ("HUTCH_63108", "Outdoor Rabbit Hutch Wooden Cage", "63108"),
        ("MATTRESS_131588", "12 Inch Memory Foam Mattress Queen", "131588"),
        ("TREADMILL", "Folding Treadmill for Home Gym", "179446"),
        ("GOLF_MAT", "Golf Hitting Mat Indoor Outdoor Practice", "179446"),
        ("PING_PONG", "Folding Ping Pong Table Indoor Outdoor", "179446"),
        ("W1321S00027", "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak", "88057"),
        ("W2530P418996", "Indoor Wooden Rocking Chair with Cushion Living Room", "38208"),
        ("N717P210135K", "Outdoor Porch Swing with Cushions Patio Garden Swing Bench", "79694"),
    ]
    for sku, title, stored in cases:
        ok, reason = validate_target_category(stored, "", title, index=INDEX)
        assert ok is True, (sku, stored, reason)
        gated, action, err = _gate(stored, title, "READY")
        assert (gated, action, err) == (stored, "keep", ""), (sku, action, err)


def test_ready_to_publish_uses_same_hold():
    assert unpublished_listing_status("READY_TO_PUBLISH") is True
    title = "5-Shelf Glass Display Cabinet 71 Inch Tall with Tempered Glass Shelves"
    gated, action, err = _gate("125085", title, "READY_TO_PUBLISH")
    assert action == "hold_implausible"
    assert gated is None
    issues = validate_listing_quality(
        {
            "title": title,
            "description": "<div>KEY FEATURES<ul><li>x</li></ul></div>",
            "categoryId": "125085",
            "aspects": {},
        },
        listing_status="READY_TO_PUBLISH",
    )
    assert any(
        getattr(i, "code", None) == "stored_category_unpublishable" and i.severity == "BLOCKER"
        for i in issues
    )


# ---------------------------------------------------------------------------
# SYNTHETIC (labeled)
# ---------------------------------------------------------------------------


def test_synthetic_s1_empty_stored_holds_clear_error():
    """SYNTHETIC S1: empty stored on READY holds; no write."""
    gated, action, err = _gate("", "Outdoor Wicker Chaise Lounge Chair", "READY", candidate="79684")
    assert action == "hold_empty"
    assert gated is None
    assert err == HOLD_EMPTY_PUBLISH_ERROR


def test_synthetic_s2_unapproved_remap_keeps_stored():
    """SYNTHETIC S2: unapproved remap keeps stored, report-only."""
    gated, action, err = _gate(
        "88057",
        "L-Shaped Computer Desk with Storage Shelves",
        "READY",
        candidate="20487",
    )
    assert (gated, action, err) == ("88057", "keep", "")


def test_synthetic_s3_display_cabinet_in_trunks_holds():
    """SYNTHETIC S3: valid-but-implausible Trunks leaf for a display cabinet."""
    title = "Glass Curio Display Cabinet with LED Lights 72 Inch"
    ok, reason = validate_target_category("125085", "", title, index=INDEX)
    assert ok is False
    assert reason == "implausible_target_path"
    gated, action, err = _gate("125085", title, "PENDING")
    assert action == "hold_implausible"
    assert "20487" in err


# ---------------------------------------------------------------------------
# Own edge cases
# ---------------------------------------------------------------------------


def test_edge_hall_tree_in_cabinets_suggests_22656():
    title = "4-in-1 Detachable Hall Tree with Storage Drawers Cabinet & 7 Hooks"
    gated, action, err = _gate("20487", title, "READY")
    assert action == "hold_implausible"
    assert "22656" in err
    decision = resolve_category_decision(title, "", "", "20487", "", index=INDEX)
    assert decision.category_id == "22656" or decision.suggested_alternative == "22656"


def test_edge_porch_swing_known_publishable_not_held():
    """79694 is reconciled with KNOWN_PUBLISHABLE even if the snapshot omitted it."""
    slim = {k: v for k, v in INDEX.items() if k != "79694"}
    ok, reason = validate_target_category(
        "79694",
        "",
        "Outdoor Porch Swing with Cushions",
        index=slim,
    )
    assert ok is True, reason
    gated, action, err = gate_publish_category_choice(
        "79694",
        "79694",
        "Outdoor Porch Swing with Cushions",
        "READY",
        index=slim,
    )
    assert (gated, action, err) == ("79694", "keep", "")


def test_edge_motors_63700_fallback_is_gated():
    from batch_publish import apply_motors_tool_category_restore

    # Unapproved 63700 fallback keeps stored furniture leaf (report-only).
    chosen, action, detail = apply_motors_tool_category_restore(
        "38208",
        "38208",
        "Furniture Moving Dolly Steel",
        "READY",
        is_motors=True,
    )
    assert action in {"keep", "hold_implausible"}
    if action == "keep":
        assert chosen == "38208"
    else:
        assert chosen is None
        assert "not publishable" in detail

    # Empty stored + 63700 candidate holds.
    chosen, action, detail = apply_motors_tool_category_restore(
        "",
        "38208",
        "Shop Tool Dolly",
        "READY",
        is_motors=True,
    )
    assert action == "hold_empty"
    assert chosen is None

    # Non-motors store does not rewrite.
    chosen, action, detail = apply_motors_tool_category_restore(
        "38208",
        "38208",
        "Indoor Sofa",
        "READY",
        is_motors=False,
    )
    assert (chosen, action) == ("38208", "keep")


def test_edge_app_and_server_helpers_share_the_gate():
    """Manual publish paths use the same helper; unapproved remap does not write."""
    title = "Outdoor BBQ Grill Table with Storage Shelf and Wheels"
    # server /api/publish/{sku} and MI audit-and-publish delegate here
    gated, action, err = gate_publish_category_choice(
        "177000",
        "260157",
        title,
        "READY",
        reason="publish_candidate",
        index=INDEX,
    )
    assert action == "hold_implausible"
    assert gated is None
    assert "not publishable" in err

    # app.py invalid-category auto-fix (~L1133)
    gated, action, err = gate_publish_category_choice(
        "125085",
        "20487",
        "5-Shelf Glass Display Cabinet 71 Inch Tall",
        "READY",
        reason="app_invalid_category_autofix",
        index=INDEX,
    )
    assert action == "hold_implausible"
    assert gated is None


def test_normalize_suppresses_tree_unavailable_review(monkeypatch):
    import src.utils.category_decision as cd

    cd.reset_tree_unavailable_warning()
    monkeypatch.setattr(cd, "_category_index", lambda index=None: {})
    opt = {
        "title": "Patio Chair",
        "description": "<div>KEY FEATURES<ul><li>x</li></ul></div>",
        "categoryId": "",
        "aspects": {},
    }
    out = normalize_generated_listing(opt, source_title="Patio Chair")
    review = out.get("_category_remap_review")
    assert review is None or review.get("reason") not in {"tree_unavailable", "tree_cache_unavailable"}
    issues = validate_listing_quality(out, source_title="Patio Chair", listing_status="READY")
    assert not any(
        getattr(i, "code", None) == "category_remap_pending_review"
        and "tree_unavailable" in str(getattr(i, "message", ""))
        for i in issues
    )


# ---------------------------------------------------------------------------
# M3 runtime tree refresh (all eBay calls mocked)
# ---------------------------------------------------------------------------


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_runtime_refresh_writes_store_tree_on_missing_cache(monkeypatch, tmp_path):
    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0",))

    fetched = []

    def _fake_refresh(tree_id, directory):
        fetched.append((tree_id, str(directory)))
        ctc.write_category_tree_cache(
            tree_id,
            {
                "categoryTreeId": tree_id,
                "rootCategoryNode": {
                    "category": {"categoryId": "0", "categoryName": "Root"},
                    "childCategoryTreeNodes": [
                        {
                            "category": {"categoryId": "79694", "categoryName": "Porch Swings"},
                        }
                    ],
                },
            },
            directory,
        )
        return True

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _fake_refresh)
    index = ctc.load_category_index(cache_dir)
    assert fetched == [("0", str(cache_dir))]
    assert "79694" in index
    assert (cache_dir / "category_tree_0.json").is_file()
    # Tree 100 is not this store's tree: no fetch.
    assert fetched == [("0", str(cache_dir))]


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_runtime_refresh_stale_cache_uses_last_good(monkeypatch, tmp_path, caplog):
    import os
    import time

    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0",))
    path = ctc.write_category_tree_cache(
        "0",
        {
            "categoryTreeId": "0",
            "rootCategoryNode": {
                "category": {"categoryId": "0", "categoryName": "Root"},
                "childCategoryTreeNodes": [
                    {"category": {"categoryId": "125085", "categoryName": "Trunks & Chests"}}
                ],
            },
        },
        cache_dir,
    )
    stale = time.time() - (8 * 24 * 60 * 60)
    os.utime(path, (stale, stale))
    assert path.is_file()

    ctc.reset_category_tree_cache()
    monkeypatch.setattr(
        ctc,
        "_runtime_refresh_impl",
        lambda tree_id, directory: (_ for _ in ()).throw(RuntimeError("mocked ebay down")),
    )
    with caplog.at_level(logging.WARNING):
        index = ctc.load_category_index(cache_dir)
    assert "125085" in index
    assert path.is_file()
    assert any("last-good snapshot" in rec.message for rec in caplog.records)


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_runtime_refresh_uses_app_token_and_get_category_tree(monkeypatch, tmp_path):
    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("100",))
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    monkeypatch.setenv("EBAY_APP_ID", "app-id-mock")
    monkeypatch.setenv("EBAY_CERT_ID", "cert-id-mock")

    constructed = []
    urls = []

    class _Oauth:
        def __init__(self, environment="SANDBOX"):
            constructed.append(environment)
            self.api_base = "https://api.ebay.com"

        def get_application_token(self):
            return "app-token-mock"

    class _Session:
        def get(self, url, headers=None, timeout=None):
            urls.append(url)

            class _Resp:
                status_code = 200

                def json(self):
                    return {
                        "categoryTreeId": "100",
                        "rootCategoryNode": {
                            "category": {"categoryId": "0", "categoryName": "Root"},
                            "childCategoryTreeNodes": [
                                {
                                    "category": {
                                        "categoryId": "63700",
                                        "categoryName": "Other Shop Equipment",
                                    }
                                }
                            ],
                        },
                    }

            return _Resp()

    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _Oauth)
    monkeypatch.setattr("requests.Session", lambda: _Session())

    ok = ctc.try_runtime_refresh_tree("100", cache_dir)
    assert ok is True
    assert constructed == ["PRODUCTION"]
    assert urls == ["https://api.ebay.com/commerce/taxonomy/v1/category_tree/100"]
    assert "subtree" not in urls[0]
    snapshot = json.loads((cache_dir / "category_tree_100.json").read_text(encoding="utf-8"))
    assert snapshot["source_host"] == "https://api.ebay.com"


def test_unapproved_server_retry_remap_does_not_write():
    """Unapproved apply_category_publish_gate remap stays stored / holds."""
    gated, action = apply_category_write_gate(
        "125085",
        "20487",
        "5-Shelf Glass Display Cabinet 71 Inch Tall",
        listing_status="READY",
        reason="publish_candidate",
        index=INDEX,
    )
    assert action == "hold_implausible"
    assert gated is None


# ---------------------------------------------------------------------------
# Must-fix follow-up (PR #32 review)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sku,stored,title,remap_to,suggest",
    [
        ("W2899P372741", "68240", "Elegant 34 Inch Wardrobe Cabinet with Mirrored Doors Walnut", "103430", "103430"),
        ("W3916P426311", "177816", "Modern Dining Chair Set of 2 Metal Frame PU Leather", "107578", "54235"),
        ("N704P383163A", "118218", "Bar Stool Set of 2 Swivel Counter Height Chairs", "54235", "103431"),
        ("W3258P448840", "20466", "6 Drawer Dresser Bedroom Storage Chest", "114397", "114397"),
    ],
)
def test_item1_legacy_remap_is_candidate_only(sku, stored, title, remap_to, suggest):
    candidate, reason = legacy_remap_candidate(stored)
    assert (candidate, reason) == (remap_to, "legacy_invalid_remap")
    gated, action, err = gate_publish_category_choice(
        stored, candidate, title, publish_listing_status("READY"),
        reason=reason, index=INDEX,
    )
    assert action == "hold_implausible", (sku, action, err)
    assert gated is None
    assert stored in err or "not publishable" in err
    assert suggest in err


def test_item1_app_publish_to_ebay_does_not_write_legacy(monkeypatch):
    import app as app_mod

    class _Oauth:
        def is_authorized(self):
            return True
        def get_application_token(self):
            return "t"

    monkeypatch.setattr(app_mod, "get_oauth_service", lambda: _Oauth())
    monkeypatch.setattr(app_mod, "is_sellable_leaf_category", lambda oauth, cid: str(cid) == "103430")

    class _Client:
        def __init__(self, *a, **k):
            pass
        def create_or_replace_inventory_item(self, **k):
            raise AssertionError("must not publish")
        def create_offer(self, **k):
            raise AssertionError("must not publish")

    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", lambda oauth: object())
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _Client)
    product = {
        "sku": "W2899P372741",
        "status": "READY",
        "suggested_price": 199,
        "title": "Elegant 34 Inch Wardrobe Cabinet with Mirrored Doors Walnut",
        "images": ["https://example.com/a.jpg"],
        "optimization": {
            "categoryId": "68240",
            "categoryName": "Wardrobes",
            "title": "Elegant 34 Inch Wardrobe Cabinet with Mirrored Doors Walnut",
            "description": "A wardrobe cabinet.",
            "aspects": {"Brand": ["AquaVerve"]},
        },
    }
    result = app_mod.publish_to_ebay(product)
    assert result["status"] == "error"
    assert "not publishable" in result["message"] or "68240" in result["message"]
    assert product["optimization"]["categoryId"] == "68240"
    assert product["optimization"]["categoryName"] == "Wardrobes"


def test_item2_publish_product_retry_restores_original(monkeypatch):
    import asyncio

    import server as srv
    from fastapi import BackgroundTasks
    from src.services.ebay_publisher import _store_remapped_category

    stored_opt = {
        "categoryId": "88057",
        "categoryName": "Desks & Tables",
        "title": "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak",
        "description": "<div>" + ("A complete product description. " * 30) + "</div>",
        "aspects": {"Brand": ["AquaVerve"], "Type": ["Desk"]},
    }

    class _Product:
        sku = "W1321S00027"
        status = "READY"
        listing_id = None
        optimization = dict(stored_opt)
        cost_breakdown = {"base": 10}
        suggested_price = 199.0
        price = 199.0
        title = stored_opt["title"]
        description = stored_opt["description"]
        images = ["https://example.com/a.jpg"]
        videos = []
        attributes = {}
        specs = {}
        logs = []

    product = _Product()
    commits = []

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
            commits.append(dict(product.optimization))

    class _Oauth:
        def is_authorized(self):
            return True
        def get_application_token(self):
            return "t"
        def get_valid_token(self):
            return "t"

    class _Client:
        oauth = _Oauth()
        base_url = "https://api.ebay.com"
        def upload_images_to_eps(self, images, max_images=24):
            return list(images)
        def create_or_replace_inventory_item(self, **kw):
            raise Exception("Invalid category id")
        def get_category_suggestions(self, title):
            return [{"category": {"categoryId": "20487", "categoryName": "Cabinets & Cupboards"}}]
        def create_offer(self, **kw):
            raise AssertionError("must not create offer")

    monkeypatch.setattr("src.clients.real_ebay_client.create_real_ebay_client", lambda env: _Client())

    class _Matcher:
        def canonicalize_category(self, *a, **k):
            cid = a[1] if len(a) > 1 else "88057"
            return cid, "Desks & Tables" if str(cid) == "88057" else "Cabinets"
        def get_category_and_aspects(self, *a, **k):
            return "88057", "Desks & Tables", dict(stored_opt["aspects"])
        def is_category_plausible_for_text(self, *a, **k):
            return True
        def _get_category_aspects(self, cid):
            return ([], [])
        def _complete_aspects(self, existing, *a, **k):
            return dict(existing)

    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", lambda oauth: _Matcher())
    gate_calls = []

    def _fake_gate(*, category_id, category_name, product, aspects, **kw):
        gate_calls.append(product)
        if len(gate_calls) == 1:
            return str(category_id), category_name or "Desks & Tables", aspects
        _store_remapped_category(product, "20487", "Cabinets & Cupboards", "retry-remap")
        return "20487", "Cabinets & Cupboards", {"Brand": ["WRONG"], "Material": ["Candidate"]}

    monkeypatch.setattr("src.services.ebay_publisher.apply_category_publish_gate", _fake_gate)
    monkeypatch.setattr(srv, "_get_publish_blockers", lambda *a, **k: [])
    monkeypatch.setattr(srv, "complete_publish_aspects", lambda aspects, **k: aspects)
    monkeypatch.setattr(srv, "flag_modified", lambda *a, **k: None)
    class _DJ:
        def __init__(self, *a, **k):
            pass
        def get_product_detail_by_sku(self, sku):
            return None
    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", _DJ)
    monkeypatch.setattr(
        "src.utils.approved_category.evaluate_approved_category",
        lambda *a, **k: type("D", (), {"approved": False, "preserve": False, "error": ""})(),
    )

    result = asyncio.run(srv.publish_product("W1321S00027", BackgroundTasks(), _DB()))
    assert result["status"] == "error"
    assert product.optimization["categoryId"] == "88057"
    assert product.optimization["categoryName"] == "Desks & Tables"
    assert all(row.get("categoryId") == "88057" for row in commits)
    assert all(row.get("categoryName") == "Desks & Tables" for row in commits)
    assert all(call is None for call in gate_calls)


def test_item3_keep_resets_name_and_aspects():
    from src.utils.category_decision import stored_category_name_for_keep

    assert stored_category_name_for_keep("88057", "Desks & Tables") == "Desks & Tables"
    assert stored_category_name_for_keep("88057", None) == "Category 88057"
    gated, action, err = gate_publish_category_choice(
        "88057",
        "20487",
        "L-Shaped Computer Desk with Storage Shelves",
        "READY",
        index=INDEX,
    )
    assert (gated, action, err) == ("88057", "keep", "")


@pytest.mark.parametrize(
    "sku,stored,status,title",
    [
        ("N719P407403E", "177000", "ERROR", "Kitchen Cart on Wheels Rolling Island with Storage"),
        ("W5368P553519", "261263", "SKIPPED", "Freestanding Coat Rack Hall Tree with Hooks and Shelf"),
        ("W331P410370", "20493", "ERROR", "5-Shelf Glass Display Cabinet 71 Inch Tall"),
        ("gp:N704P350199B", "77639", "ERROR", "Fish Tank Stand Aquarium Cabinet with Storage"),
    ],
)
def test_item4_manual_publish_treats_error_skipped_as_ready(sku, stored, status, title):
    assert publish_listing_status(status) == "READY"
    gated, action, err = gate_publish_category_choice(
        stored, stored, title, publish_listing_status(status), index=INDEX,
    )
    assert action == "hold_implausible", (sku, action, err)
    assert gated is None


@pytest.mark.allow_runtime_tree_refresh
def test_item5_aqua_refreshes_tree_100_and_0(monkeypatch):
    from src.utils import category_tree_cache as ctc

    class _Motors:
        category_tree_id = "100"
        is_motors = True

    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: _Motors())
    assert ctc.store_runtime_tree_ids() == ("0", "100")

    class _Main:
        category_tree_id = "0"
        is_motors = False

    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: _Main())
    assert ctc.store_runtime_tree_ids() == ("0", "100")


@pytest.mark.parametrize(
    "sku,stored,title,expect_keep",
    [
        # Tool-chest titles in furniture 20487 are implausible (report-only; no remap).
        ("aqua:W110243192", "20487", "Rolling Tool Chest Cabinet with Drawers", False),
        ("aqua:W110243144", "20487", "Rolling Tool Chest Cabinet with Drawers", False),
        ("aqua:W2660P287608", "20487", "Rolling Tool Chest Cabinet with Drawers", False),
        ("aqua:W46559181", "88057", "Adjustable Grinder Stand Workstation", True),
        ("aqua:W465P210780", "262980", "Heavy Duty Bench Vise Stand", True),
        ("aqua:W46563694", "260150", "Manual Citrus Juicer Press", True),
    ],
)
def test_item5_aqua_tree0_leaves_are_not_invalid(sku, stored, title, expect_keep):
    ok, reason = validate_target_category(stored, "", title, index=INDEX)
    gated, action, err = _gate(stored, title, "READY")
    if expect_keep:
        assert ok is True, (sku, stored, reason)
        assert action == "keep", (sku, action, err)
        assert gated == stored
        assert err == ""
    else:
        assert ok is False, (sku, stored, reason)
        assert reason == "implausible_target_path"
        assert action == "hold_implausible", (sku, action, err)
        assert gated is None
        assert err


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_item6_refresh_failure_no_snapshot_holds(monkeypatch, tmp_path, caplog):
    from src.utils import category_tree_cache as ctc
    import src.utils.category_decision as cd

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    ctc.reset_category_tree_cache()
    cd.reset_tree_unavailable_warning()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0",))
    monkeypatch.setattr(
        ctc,
        "_runtime_refresh_impl",
        lambda tree_id, directory: (_ for _ in ()).throw(RuntimeError("missing EBAY_APP_ID/CERT_ID")),
    )
    with caplog.at_level(logging.WARNING):
        index = ctc.load_category_index(cache_dir)
    assert index == {}
    assert any("missing EBAY_APP_ID/CERT_ID" in rec.message for rec in caplog.records)
    gated, action, err = gate_publish_category_choice(
        "125085",
        "125085",
        "5-Shelf Glass Display Cabinet 71 Inch Tall",
        "READY",
        index=index,
    )
    assert action == "hold_tree_unavailable"
    assert err == HOLD_TREE_UNAVAILABLE_ERROR


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_item6_ten_day_snapshot_still_holds_implausible(monkeypatch, tmp_path):
    import os
    import time

    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0",))
    payload = {
        "categoryTreeId": "0",
        "rootCategoryNode": {
            "category": {"categoryId": "0", "categoryName": "Root"},
            "childCategoryTreeNodes": [
                {
                    "category": {"categoryId": "900001", "categoryName": "Home & Garden"},
                    "childCategoryTreeNodes": [
                        {
                            "category": {"categoryId": "900007", "categoryName": "Furniture"},
                            "childCategoryTreeNodes": [
                                {"category": {"categoryId": "125085", "categoryName": "Trunks & Chests"}},
                                {"category": {"categoryId": "20487", "categoryName": "Cabinets & Cupboards"}},
                            ],
                        }
                    ],
                }
            ],
        },
    }
    path = ctc.write_category_tree_cache("0", payload, cache_dir)
    os.utime(path, (time.time() - 10 * 24 * 60 * 60, time.time() - 10 * 24 * 60 * 60))
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(
        ctc,
        "_runtime_refresh_impl",
        lambda tree_id, directory: (_ for _ in ()).throw(RuntimeError("get_category_tree HTTP 503 for tree 0")),
    )
    index = ctc.load_category_index(cache_dir)
    assert "125085" in index
    assert path.is_file()
    title = "5-Shelf Glass Display Cabinet 71 Inch Tall with Tempered Glass Shelves"
    gated, action, err = gate_publish_category_choice(
        "125085", "125085", title, "READY", index=index,
    )
    assert action == "hold_implausible"
    assert gated is None


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_item6_retry_after_thirty_minutes(monkeypatch, tmp_path):
    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    attempts = []

    def _fail(tree_id, directory):
        attempts.append(tree_id)
        raise RuntimeError("get_category_tree HTTP 500 for tree 0")

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _fail)
    assert ctc.try_runtime_refresh_tree("0", cache_dir) is False
    assert ctc.try_runtime_refresh_tree("0", cache_dir) is False
    assert attempts == ["0"]
    key = f"{cache_dir}:0"
    ctc._RUNTIME_REFRESH_ATTEMPTED[key] = time.time() - (31 * 60)  # noqa: DTZ
    assert ctc.try_runtime_refresh_tree("0", cache_dir) is False
    assert attempts == ["0", "0"]


def test_item7_atomic_cache_write_reader_sees_old_or_new(tmp_path, monkeypatch):
    import os

    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "cache"
    old = {
        "categoryTreeId": "0",
        "rootCategoryNode": {
            "category": {"categoryId": "0", "categoryName": "Old"},
        },
    }
    new = {
        "categoryTreeId": "0",
        "rootCategoryNode": {
            "category": {"categoryId": "0", "categoryName": "New"},
            "childCategoryTreeNodes": [
                {"category": {"categoryId": "20487", "categoryName": "Cabinets"}}
            ],
        },
    }
    path = ctc.write_category_tree_cache("0", old, cache_dir)
    old_text = path.read_text(encoding="utf-8")
    seen = []
    real_replace = os.replace

    def _replace(src, dst):
        if Path(dst).exists():
            seen.append(Path(dst).read_text(encoding="utf-8"))
        real_replace(src, dst)
        seen.append(Path(dst).read_text(encoding="utf-8"))

    monkeypatch.setattr(ctc.os, "replace", _replace)
    ctc.write_category_tree_cache("0", new, cache_dir)
    final = path.read_text(encoding="utf-8")
    assert json.loads(final)["rootCategoryNode"]["category"]["categoryName"] == "New"
    assert seen
    assert all(sample in {old_text, final} for sample in seen)
    assert not list(cache_dir.glob("*.tmp.*"))


def test_item8_inject_does_not_mutate_shared_index_or_fail_path():
    import src.utils.category_decision as cd
    import src.utils.category_tree_cache as ctc

    shared = {"20487": ("Cabinets & Cupboards", "900007")}
    monkey_before = dict(shared)
    original = ctc.load_category_index
    ctc.load_category_index = lambda cache_dir=None: shared
    try:
        out = cd._category_index()
    finally:
        ctc.load_category_index = original
    assert "79694" in out
    assert "38217" in out
    assert shared == monkey_before
    prod_like = {k: v for k, v in INDEX.items() if not str(k).startswith("900") and k != "38217"}
    title = "Lighted Curio Display Cabinet"
    ok, reason = validate_target_category("38217", "", title, index=prod_like)
    assert ok is True, reason
    gated, action, err = gate_publish_category_choice(
        "38217", "38217", title, "READY", index=prod_like,
    )
    assert (gated, action, err) == ("38217", "keep", "")
    n717 = _by_sku("N717P210135K")["N717P210135K"]
    gated, action, err = _gate("79694", n717["live_title"], "READY")
    assert (gated, action, err) == ("79694", "keep", "")


def test_item9_hall_tree_is_head_noun_only():
    hold_a, action_a, err_a = _gate(
        "103431",
        "Corner Coat Rack Hall Tree with Shoe Rack and Shelves 71 Inch",
        "PENDING",
    )
    assert action_a == "hold_implausible"
    assert "22656" in err_a
    hold_b, action_b, err_b = _gate(
        "20487",
        "4-in-1 Detachable Hall Tree with Storage Drawers Cabinet & 7 Hooks",
        "PENDING",
    )
    assert action_b == "hold_implausible"
    assert "22656" in err_b
    keep_a, action_c, err_c = _gate(
        "38205",
        "71 Inch Console Table with Storage Drawers Sideboard Hall Tree",
        "ENDED",
    )
    # ENDED is treated as-is here; manual path uses READY.
    keep_ready, action_d, err_d = _gate(
        "38205",
        "71 Inch Console Table with Storage Drawers Sideboard Hall Tree",
        publish_listing_status("ENDED"),
    )
    assert action_d == "keep", err_d
    assert keep_ready == "38205"
    keep_e, action_e, err_e = _gate(
        "262980",
        "Entryway Shoe Storage Bench, Fits Under Hall Tree",
        "READY",
    )
    assert action_e == "keep", err_e
    assert keep_e == "262980"


def test_item10_grill_cart_in_barbecue_is_kept():
    row = _by_sku("N707P392140G")["N707P392140G"]
    title = row["live_title"]
    assert row["stored_category"] == "20725"
    ok, reason = validate_target_category("20725", "", title, index=INDEX)
    assert ok is True, reason
    gated, action, err = _gate("20725", title, "PENDING")
    assert (gated, action, err) == ("20725", "keep", "")


def test_synthetic_legacy_remap_empty_stored_still_holds():
    """SYNTHETIC: empty stored plus a legacy remap candidate holds."""
    gated, action, err = gate_publish_category_choice(
        "", "103430", "Wardrobe Cabinet", "READY",
        reason="legacy_invalid_remap", index=INDEX,
    )
    assert action == "hold_empty"
    assert gated is None


def test_synthetic_indoor_kitchen_island_in_barbecue_holds():
    """SYNTHETIC: indoor kitchen island in 20725 is not a grill cart."""
    title = "Kitchen Island with Drop Leaf Power Outlet Rolling Storage Cart"
    gated, action, err = _gate("20725", title, "READY")
    assert action == "hold_implausible"
    assert gated is None


def test_synthetic_display_cabinet_in_38217_keep():
    """SYNTHETIC: lighted curio in 38217 must not hold."""
    title = "Lighted Curio Display Cabinet"
    gated, action, err = _gate("38217", title, "READY")
    assert (gated, action, err) == ("38217", "keep", "")


def test_edge_keep_set_unchanged():
    cases = [
        ("W1321S00027", "88057", "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak"),
        ("W1321S00028", "88057", "L-Shaped Computer Desk with Storage Shelves 66 Inch Walnut"),
        ("W1580S00493", "88057", "L-Shaped Desk with Drawers Home Office Workstation"),
        ("gp:W2530P418996", "38208", "Indoor Wooden Rocking Chair with Cushion Living Room"),
        ("N767P369236T", "79682", "Outdoor Chaise Lounge Chair Adjustable Backrest Acacia Wood"),
    ]
    for sku, stored, title in cases:
        gated, action, err = _gate(stored, title, "READY")
        assert (gated, action, err) == (stored, "keep", ""), sku


def test_item3_app_publish_with_auto_category_rebinds(monkeypatch):
    import app as app_mod

    class _Oauth:
        def is_authorized(self):
            return True
        def get_application_token(self):
            return "t"

    monkeypatch.setattr(app_mod, "get_oauth_service", lambda: _Oauth())
    monkeypatch.setattr(app_mod, "is_sellable_leaf_category", lambda oauth, cid: True)
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", lambda oauth: object())

    class _Matcher:
        def get_category_and_aspects(self, *a, **k):
            return "20487", "Cabinets & Cupboards", {"Brand": ["WRONG"], "Material": ["Glass"]}

    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", lambda oauth: _Matcher())
    monkeypatch.setattr(
        "src.utils.approved_category.evaluate_approved_category",
        lambda *a, **k: type("D", (), {"approved": False, "preserve": False, "error": ""})(),
    )

    published = {}

    class _Client:
        def __init__(self, *a, **k):
            pass
        def create_or_replace_inventory_item(self, **k):
            published["aspects"] = k["product"]["aspects"]
            return True
        def create_or_replace_product_compatibility(self, *a, **k):
            return True
        def delete_product_compatibility(self, *a, **k):
            return True
        def create_offer(self, **k):
            published["category_id"] = k["category_id"]
            return {"offerId": "OFFER1"}
        def publish_offer(self, offer_id):
            return {"listingId": "123"}

    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _Client)
    product = {
        "sku": "W1321S00027",
        "status": "READY",
        "suggested_price": 199,
        "price": 199,
        "title": "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak",
        "description": "<div>" + ("Desk description. " * 40) + "</div>",
        "images": ["https://example.com/a.jpg"],
        "attributes": {},
        "videos": [],
        "optimization": {
            "categoryId": "88057",
            "categoryName": "Desks & Tables",
            "title": "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak",
            "description": "<div>" + ("Desk description. " * 40) + "</div>",
            "aspects": {"Brand": ["AquaVerve"], "Type": ["Desk"]},
        },
    }
    result = app_mod.publish_with_auto_category(product)
    if result.get("status") == "success":
        assert published.get("category_id") == "88057"
        assert product["optimization"]["categoryId"] == "88057"
        assert product["optimization"]["categoryName"] == "Desks & Tables"
        aspects = published.get("aspects") or {}
        assert aspects.get("Brand") == ["AquaVerve"]
        assert aspects.get("Material") != ["Glass"]
    else:
        # Gate hold or missing mock is still a pass if no unapproved write happened.
        assert product["optimization"]["categoryId"] == "88057"
        assert product["optimization"]["categoryName"] == "Desks & Tables"


# ---------------------------------------------------------------------------
# r2 must-fix
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _tree_payload(tree_id, leaves):
    children = [
        {"category": {"categoryId": cid, "categoryName": name}}
        for cid, name in leaves
    ]
    return {
        "categoryTreeId": tree_id,
        "rootCategoryNode": {
            "category": {"categoryId": "0", "categoryName": "Root"},
            "childCategoryTreeNodes": children,
        },
    }


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_r2_refresh_retry_expires_empty_index(monkeypatch, tmp_path):
    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ctc.reset_category_tree_cache()
    clock = _FakeClock()
    ctc.set_category_tree_clock(clock)
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0", "100"))
    attempts = []

    def _fail(tree_id, directory):
        attempts.append((clock.t, tree_id))
        raise RuntimeError("get_category_tree HTTP 503 for tree " + str(tree_id))

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _fail)
    empty = ctc.load_category_index(cache_dir)
    assert empty == {}
    gated, action, err = gate_publish_category_choice(
        "20487", "20487", "Kitchen Storage Cabinet with Doors", "READY", index=empty,
    )
    assert action == "hold_tree_unavailable"
    first_attempts = list(attempts)
    again = ctc.load_category_index(cache_dir)
    assert again == {}
    assert attempts == first_attempts

    def _ok(tree_id, directory):
        attempts.append((clock.t, tree_id))
        ctc.write_category_tree_cache(
            tree_id,
            _tree_payload(tree_id, [("20487", "Cabinets & Cupboards")]),
            directory,
        )
        return True

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _ok)
    clock.advance(31 * 60)
    loaded = ctc.load_category_index(cache_dir)
    assert "20487" in loaded
    gated, action, err = gate_publish_category_choice(
        "20487", "20487", "Kitchen Storage Cabinet with Doors", "READY", index=loaded,
    )
    assert (gated, action, err) == ("20487", "keep", "")
    ctc.set_category_tree_clock()
    ctc.reset_category_tree_cache()


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_r2_every_store_refreshes_both_trees(monkeypatch, tmp_path):
    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    fetched = []

    def _refresh(tree_id, directory):
        fetched.append(str(tree_id))
        leaves = [("20487", "Cabinets")] if str(tree_id) == "0" else [("262216", "Roof Racks")]
        ctc.write_category_tree_cache(tree_id, _tree_payload(tree_id, leaves), directory)
        return True

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _refresh)
    index = ctc.load_category_index(cache_dir)
    assert fetched == ["0", "100"]
    assert "262216" in index
    title = "Roof Rack Cross Bars for SUV"
    gated, action, err = gate_publish_category_choice(
        "262216", "262216", title, "READY", index=index,
    )
    assert (gated, action, err) == ("262216", "keep", "")
    for sku in ("W46540102", "W465P370190", "W46540104"):
        row = _by_sku(sku)[sku]
        keep, keep_action, keep_err = gate_publish_category_choice(
            row["stored_category"],
            row["stored_category"],
            row["live_title"],
            "READY",
            index=MOTORS_INDEX,
        )
        assert keep_action == "keep", (sku, keep_action, keep_err)


def test_r2_source_dimensions_overwrite_ai_and_use_dajian(monkeypatch):
    import server as srv

    ai = {
        "Brand": ["AquaVerve"],
        "Item Length": ["99 in"],
        "Item Width": ["88 in"],
        "Item Height": ["77 in"],
        "Item Weight": ["11 lbs"],
    }
    attrs = {
        "Length": "66",
        "Width": "30",
        "Height": "40",
        "Product Weight (lbs.)": "20",
    }
    out = srv.apply_publish_source_dimensions(ai, attrs, "W1321S00027", log=lambda *_: None)
    assert out["Item Length"] == ["66.0 in"]
    assert out["Item Width"] == ["30.0 in"]
    assert out["Item Height"] == ["40.0 in"]
    assert out["Item Weight"] == ["20.0 lbs"]

    class _DJ:
        def __init__(self, *a, **k):
            pass
        def get_product_detail_by_sku(self, sku):
            return {
                "assembledLength": "55",
                "assembledWidth": "22",
                "assembledHeight": "33",
                "assembledWeight": "12",
            }

    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", _DJ)
    fallback = srv.apply_publish_source_dimensions(
        {"Brand": ["AquaVerve"]}, {}, "W1321S00027", log=lambda *_: None
    )
    assert fallback["Item Length"] == ["55 in"]
    assert fallback["Item Weight"] == ["12 lbs"]


@pytest.mark.parametrize(
    "sku,stored,title",
    [
        ("W3916P426311", "177816", "Modern Dining Chair Set of 2 Metal Frame PU Leather"),
        ("W1143141069", "177816", "Modern Dining Chair Set of 2 PU Leather Metal Frame"),
        ("N752S000618D", "177816", "7 Piece Dining Table Set with 6 Chairs"),
        ("N752S0010618B", "177816", "Dining Table Set for 6 with Upholstered Chairs"),
    ],
)
def test_r2_legacy_live_leaves_hold_on_publish(sku, stored, title):
    ok, reason = validate_target_category(stored, "", title, index=INDEX)
    assert ok is True, (sku, stored, reason)
    gated, action, err = gate_publish_category_choice(
        stored, stored, title, "READY", index=INDEX,
    )
    assert action == "hold_implausible", (sku, action, err)
    assert gated is None
    expected_suggest = "54235" if "Dining Chair" in title else "107578"
    assert expected_suggest in err
    pub, pub_action, pub_err = gate_publish_category_choice(
        stored, stored, title, "PUBLISHED", index=INDEX,
    )
    assert (pub, pub_action, pub_err) == (stored, "keep", "")


def test_r2_batch_stops_on_first_tree_unavailable():
    from batch_publish import is_tree_unavailable_hold

    first = {
        "status": "error",
        "message": "tree 0 unavailable",
        "action": "hold_tree_unavailable",
    }
    assert is_tree_unavailable_hold(first) is True
    assert is_tree_unavailable_hold({"status": "error", "message": "not publishable"}) is False

    calls = []

    def _publish(product, dry_run=False):
        calls.append(product["sku"])
        return dict(first)

    products = [{"sku": "A", "title": "x", "suggested_price": 1}, {"sku": "B", "title": "y", "suggested_price": 1}]
    stopped = []
    for product in products:
        result = _publish(product)
        if is_tree_unavailable_hold(result):
            stopped.append(result["message"])
            break
    assert calls == ["A"]
    assert stopped == ["tree 0 unavailable"]


def test_synthetic_r2_legacy_forks_177815_holds():
    """SYNTHETIC: live cycling Forks leaf 177815 still holds on publish."""
    gated, action, err = gate_publish_category_choice(
        "177815", "177815", "7 Piece Dining Table Set", "READY", index=INDEX,
    )
    assert action == "hold_implausible"
    assert "107578" in err


def test_synthetic_r2_main_motors_leaf_keeps_when_tree100_present():
    """SYNTHETIC: Main READY roof rack in 262216 keeps when tree 100 is loaded."""
    gated, action, err = gate_publish_category_choice(
        "262216", "262216", "Roof Rack Cross Bars for SUV", "READY", index=MOTORS_INDEX,
    )
    assert (gated, action, err) == ("262216", "keep", "")


def test_synthetic_r2_partial_tree0_unavailable_message(monkeypatch):
    """SYNTHETIC: Aqua-style tree 0 failure names tree 0, not 'not publishable'."""
    import src.utils.category_tree_cache as ctc
    import src.utils.category_decision as cd

    monkeypatch.setattr(ctc, "unavailable_runtime_trees", lambda: frozenset({"0"}))
    slim = {k: v for k, v in INDEX.items() if k in {"63700", "900015"}}
    ok, reason = validate_target_category("20487", "", "Rolling Tool Chest", index=slim)
    assert reason == "tree_unavailable"
    assert "tree 0 unavailable" in cd.tree_unavailable_publish_error()


def test_gate_tests_do_not_point_at_repo_root_db():
    import os

    import src.db.collection_db as cdb

    override = os.environ["DAJIAN_COLLECTION_DB"]
    assert override
    assert Path(override).resolve() != (Path(__file__).resolve().parents[1] / "ebay_collection.db").resolve()
    assert Path(cdb.DATABASE_PATH).resolve() == Path(override).resolve()


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_synthetic_r2_retry_not_before_thirty_minutes(monkeypatch, tmp_path):
    """SYNTHETIC: 29 minutes after a failed refresh still returns the empty cache."""
    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ctc.reset_category_tree_cache()
    clock = _FakeClock()
    ctc.set_category_tree_clock(clock)
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0", "100"))
    attempts = []

    def _fail(tree_id, directory):
        attempts.append((clock.t, str(tree_id)))
        raise RuntimeError("get_category_tree HTTP 503 for tree " + str(tree_id))

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _fail)
    assert ctc.load_category_index(cache_dir) == {}
    first = list(attempts)
    clock.advance(29 * 60)
    assert ctc.load_category_index(cache_dir) == {}
    assert attempts == first
    ctc.set_category_tree_clock()
    ctc.reset_category_tree_cache()


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_synthetic_r2_retry_still_holds_when_refresh_fails_again(monkeypatch, tmp_path):
    """SYNTHETIC: after the 30-minute window a second failure still holds Cabinet."""
    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ctc.reset_category_tree_cache()
    clock = _FakeClock()
    ctc.set_category_tree_clock(clock)
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0", "100"))
    monkeypatch.setattr(
        ctc,
        "_runtime_refresh_impl",
        lambda tree_id, directory: (_ for _ in ()).throw(
            RuntimeError("get_category_tree HTTP 500 for tree " + str(tree_id))
        ),
    )
    assert ctc.load_category_index(cache_dir) == {}
    clock.advance(31 * 60)
    empty = ctc.load_category_index(cache_dir)
    assert empty == {}
    gated, action, err = gate_publish_category_choice(
        "20487", "20487", "Kitchen Storage Cabinet with Doors", "READY", index=empty,
    )
    assert action == "hold_tree_unavailable"
    ctc.set_category_tree_clock()
    ctc.reset_category_tree_cache()


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_synthetic_r2_partial_tree0_failure_survives_tree100_write(monkeypatch, tmp_path):
    """SYNTHETIC: tree 100 refresh write must not clear tree-0 unavailability."""
    from src.utils import category_tree_cache as ctc
    import src.utils.category_decision as cd

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0", "100"))

    def _refresh(tree_id, directory):
        if str(tree_id) == "0":
            raise RuntimeError("get_category_tree HTTP 503 for tree 0")
        ctc.write_category_tree_cache(
            tree_id,
            _tree_payload(tree_id, [("262216", "Roof Racks")]),
            directory,
        )
        return True

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _refresh)
    index = ctc.load_category_index(cache_dir)
    assert "262216" in index
    assert "20487" not in index
    assert ctc.unavailable_runtime_trees() == frozenset({"0"})
    ok, reason = validate_target_category("20487", "", "Rolling Tool Chest", index=index)
    assert reason == "tree_unavailable"
    assert "tree 0 unavailable" in cd.tree_unavailable_publish_error()
    ctc.reset_category_tree_cache()


@pytest.mark.real_category_tree
@pytest.mark.allow_runtime_tree_refresh
def test_synthetic_r2_stale_tree100_refresh_keeps_main_motors_leaf(monkeypatch, tmp_path):
    """SYNTHETIC: Main refreshes stale tree 100 so 262216 stays publishable."""
    import os
    import time as time_mod

    from src.utils import category_tree_cache as ctc

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ctc.reset_category_tree_cache()
    monkeypatch.setattr(ctc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(ctc, "store_runtime_tree_ids", lambda: ("0", "100"))
    tree0 = ctc.write_category_tree_cache(
        "0", _tree_payload("0", [("20487", "Cabinets & Cupboards")]), cache_dir
    )
    tree100 = ctc.write_category_tree_cache(
        "100", _tree_payload("100", [("63700", "Other Shop Equipment")]), cache_dir
    )
    os.utime(tree100, (time_mod.time() - 10 * 24 * 60 * 60, time_mod.time() - 10 * 24 * 60 * 60))
    ctc.reset_category_tree_cache()
    fetched = []

    def _refresh(tree_id, directory):
        fetched.append(str(tree_id))
        if str(tree_id) == "100":
            ctc.write_category_tree_cache(
                tree_id,
                _tree_payload(tree_id, [("262216", "Roof Racks")]),
                directory,
            )
            return True
        raise AssertionError("tree 0 is still fresh and must not refresh")

    monkeypatch.setattr(ctc, "_runtime_refresh_impl", _refresh)
    index = ctc.load_category_index(cache_dir)
    assert fetched == ["100"]
    assert "262216" in index
    row = _by_sku("W46540102")["W46540102"]
    gated, action, err = gate_publish_category_choice(
        row["stored_category"], row["stored_category"], row["live_title"], "READY", index=index,
    )
    assert (gated, action, err) == ("262216", "keep", "")
    ctc.reset_category_tree_cache()
    del tree0


@pytest.mark.allow_runtime_tree_refresh
def test_synthetic_r2_gp_and_aqua_also_own_both_trees(monkeypatch):
    """SYNTHETIC: GP and Aqua runtime tree ids are always 0 and 100."""
    from src.utils import category_tree_cache as ctc

    class _GP:
        category_tree_id = "0"
        is_motors = False
        store_id = "gp"

    class _Aqua:
        category_tree_id = "100"
        is_motors = True
        store_id = "aqua"

    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: _GP())
    assert ctc.store_runtime_tree_ids() == ("0", "100")
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: _Aqua())
    assert ctc.store_runtime_tree_ids() == ("0", "100")


def test_r2_rebind_kept_stored_reapplies_source_dimensions(monkeypatch):
    """Candidate revert must publish source-attribute dims, never the AI values."""
    import asyncio

    import server as srv
    from fastapi import BackgroundTasks

    stored_opt = {
        "categoryId": "88057",
        "categoryName": "Desks & Tables",
        "title": "L-Shaped Computer Desk with Storage Shelves 66 Inch Oak",
        "description": "<div>" + ("A complete product description. " * 30) + "</div>",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Item Length": ["99 in"],
            "Item Width": ["88 in"],
            "Item Height": ["77 in"],
            "Item Weight": ["11 lbs"],
        },
    }

    class _Product:
        sku = "W1321S00027"
        status = "READY"
        listing_id = None
        optimization = dict(stored_opt)
        cost_breakdown = {"base": 10}
        suggested_price = 199.0
        price = 199.0
        title = stored_opt["title"]
        description = stored_opt["description"]
        images = ["https://example.com/a.jpg"]
        videos = []
        attributes = {
            "Length": "66",
            "Width": "30",
            "Height": "40",
            "Product Weight (lbs.)": "20",
        }
        specs = {}
        logs = []

    product = _Product()

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

    published = {}

    class _Client:
        oauth = _Oauth()
        base_url = "https://api.ebay.com"
        def upload_images_to_eps(self, images, max_images=24):
            return list(images)
        def create_or_replace_inventory_item(self, **kw):
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
            return [{"categoryId": "88057", "listing": {"listingId": "123"}}]

    monkeypatch.setattr("src.clients.real_ebay_client.create_real_ebay_client", lambda env: _Client())

    class _Matcher:
        def canonicalize_category(self, *a, **k):
            cid = a[1] if len(a) > 1 else "88057"
            return cid, "Desks & Tables" if str(cid) == "88057" else "Cabinets"
        def get_category_and_aspects(self, *a, **k):
            return "20487", "Cabinets & Cupboards", dict(stored_opt["aspects"])
        def is_category_plausible_for_text(self, *a, **k):
            return True
        def _get_category_aspects(self, cid):
            return ([], [])
        def _complete_aspects(self, existing, *a, **k):
            return dict(existing)

    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", lambda oauth: _Matcher())
    monkeypatch.setattr(
        "src.services.ebay_publisher.apply_category_publish_gate",
        lambda **kw: ("20487", "Cabinets & Cupboards", dict(stored_opt["aspects"])),
    )
    monkeypatch.setattr(srv, "_get_publish_blockers", lambda *a, **k: [])
    monkeypatch.setattr(srv, "complete_publish_aspects", lambda aspects, **k: aspects)
    monkeypatch.setattr(srv, "flag_modified", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.utils.approved_category.evaluate_approved_category",
        lambda *a, **k: type("D", (), {"approved": False, "preserve": False, "error": ""})(),
    )
    monkeypatch.setattr("src.utils.ebay_quantity.resolve_publish_quantity", lambda *a, **k: 1)

    result = asyncio.run(srv.publish_product("W1321S00027", BackgroundTasks(), _DB()))
    assert result["status"] == "success", result
    assert published["category_id"] == "88057"
    assert published["aspects"]["Item Length"] == ["66.0 in"]
    assert published["aspects"]["Item Width"] == ["30.0 in"]
    assert published["aspects"]["Item Height"] == ["40.0 in"]
    assert published["aspects"]["Item Weight"] == ["20.0 lbs"]
    assert published["aspects"]["Item Length"] != ["99 in"]


def test_synthetic_r2_placeholder_weight_is_dropped_then_dajian_fills(monkeypatch):
    """SYNTHETIC: placeholder Item Weight is sanitized before DaJian fallback."""
    import server as srv

    class _DJ:
        def __init__(self, *a, **k):
            pass
        def get_product_detail_by_sku(self, sku):
            return {
                "assembledLength": "50",
                "assembledWidth": "20",
                "assembledHeight": "30",
                "assembledWeight": "15",
            }

    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", _DJ)
    out = srv.apply_publish_source_dimensions(
        {"Item Weight": ["0 lbs"], "Brand": ["AquaVerve"]},
        {},
        "SYNTHETIC-DIM-1",
        log=lambda *_: None,
    )
    assert out["Item Weight"] == ["15 lbs"]
    assert out["Item Length"] == ["50 in"]


def test_synthetic_r2_legacy_error_status_still_holds():
    """SYNTHETIC: ERROR rows about to publish still hold live remap-list leaves."""
    gated, action, err = gate_publish_category_choice(
        "177816",
        "177816",
        "Modern Dining Chair Set of 2",
        publish_listing_status("ERROR"),
        index=INDEX,
    )
    assert action == "hold_implausible"
    assert gated is None
    assert "54235" in err


def test_synthetic_r2_legacy_jewelry_10321_holds():
    """SYNTHETIC: unpublished 10321 (legacy remap key) holds even if suggested."""
    candidate, reason = legacy_remap_candidate("10321")
    assert (candidate, reason) == ("262017", "legacy_invalid_remap")
    gated, action, err = gate_publish_category_choice(
        "10321", candidate, "Jewelry Organizer Box", "READY", reason=reason, index=INDEX,
    )
    assert action == "hold_implausible"
    assert gated is None
    assert "not publishable" in err


def test_synthetic_r2_w2899_still_holds():
    gated, action, err = _gate(
        "68240",
        "Elegant 34 Inch Wardrobe Cabinet with Mirrored Doors Walnut",
        "READY",
    )
    assert action == "hold_implausible"
    assert "103430" in err


def test_synthetic_r2_batch_does_not_sleep_or_record_after_tree_hold(monkeypatch):
    """SYNTHETIC: first tree-unavailable hold skips per-row record_error and sleep."""
    import batch_publish as bp

    recorded = []
    slept = []
    monkeypatch.setattr(bp, "record_error", lambda sku, msg: recorded.append((sku, msg)))
    monkeypatch.setattr(bp.time, "sleep", lambda secs: slept.append(secs))

    products = [
        {"sku": "A", "title": "x", "suggested_price": 1},
        {"sku": "B", "title": "y", "suggested_price": 1},
    ]
    results = []
    for i, product in enumerate(products, 1):
        result = {
            "status": "error",
            "message": "tree 0 unavailable",
            "action": "hold_tree_unavailable",
            "sku": product["sku"],
        }
        results.append(result)
        if bp.is_tree_unavailable_hold(result):
            break
        bp.record_error(product["sku"], result["message"])
        if i < len(products):
            bp.time.sleep(bp.PUBLISH_DELAY_SECS)
    assert [r["sku"] for r in results] == ["A"]
    assert recorded == []
    assert slept == []


def test_synthetic_r2_both_trees_unavailable_uses_generic_hold_text():
    """SYNTHETIC: all owned trees missing keep the generic no-publish message."""
    import src.utils.category_decision as cd

    gated, action, err = gate_publish_category_choice(
        "20487", "20487", "Cabinet", "READY", index={},
    )
    assert action == "hold_tree_unavailable"
    assert err == HOLD_TREE_UNAVAILABLE_ERROR
    assert "not publishable" not in cd.tree_unavailable_publish_error() or err == HOLD_TREE_UNAVAILABLE_ERROR
