"""M1/M2/M3 follow-up: manual publish gate, title-family hold, runtime tree refresh."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.utils.category_decision import (
    HOLD_EMPTY_PUBLISH_ERROR,
    apply_category_write_gate,
    gate_publish_category_choice,
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
def test_runtime_refresh_stale_cache_and_fail_closed_once(monkeypatch, tmp_path, caplog):
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
            },
        },
        cache_dir,
    )
    stale = time.time() - (8 * 24 * 60 * 60)
    os_utime = path.stat()
    path.chmod(path.stat().st_mode)
    import os

    os.utime(path, (stale, stale))
    assert os_utime

    ctc.reset_category_tree_cache()
    monkeypatch.setattr(
        ctc,
        "_runtime_refresh_impl",
        lambda tree_id, directory: (_ for _ in ()).throw(RuntimeError("mocked ebay down")),
    )
    with caplog.at_level(logging.WARNING):
        index = ctc.load_category_index(cache_dir)
        again = ctc.load_category_index(cache_dir)
    assert index == {}
    assert again == {}
    warnings = [rec.message for rec in caplog.records if "runtime refresh failed" in rec.message.lower() or "tree cache missing or stale" in rec.message.lower()]
    assert len(warnings) == 1


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
