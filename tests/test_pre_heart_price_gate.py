"""Pre-heart price gate: drop FLOOR_OVER_MARKET_LOCK before heart/collect."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.services.conversion_diagnoser import FLOOR_OVER_MARKET_LOCK as DIAG_LOCK
from src.utils.copy_skip_gate import NEAR_MARKET_BAND, safe_15_price
from src.utils.pre_heart_price_gate import (
    FLOOR_OVER_MARKET_LOCK,
    REASON_FLOOR_OVER_MARKET_LOCK,
    browse_cache_path,
    evaluate_pre_heart_price,
    extract_search_keywords,
    filter_pre_heart_items,
    lookup_cached_market_median,
)


def test_band_aliases_match_standing_lock():
    assert FLOOR_OVER_MARKET_LOCK == NEAR_MARKET_BAND == DIAG_LOCK == 1.30


def test_extract_search_keywords_matches_qwen_shape():
    assert extract_search_keywords("Black Electric Standing Desk Home Office") == "Standing Desk"
    assert extract_search_keywords(
        "52 Inch Decorative Tropical Ceiling Fan with Light and Remote"
    ) == "Tropical Ceiling Fan"
    assert "Inch" not in extract_search_keywords(
        "52 Inch Decorative Tropical Ceiling Fan with Light and Remote"
    )
    bookcase_kw = extract_search_keywords(
        "Bookcase with Doors, 5-Tier Freestanding Bookshelf"
    )
    assert "Bookcase" in bookcase_kw or "bookcase" in bookcase_kw.lower()
    assert extract_search_keywords(
        "6FT Artificial Ficus Tree in White Pot for Indoor Covered Outdoor"
    ) == "Artificial Ficus Tree"
    assert extract_search_keywords("") == ""


def test_no_market_cache_allows_with_flag(tmp_path: Path):
    decision = evaluate_pre_heart_price(
        title="Electric Standing Desk Home Office",
        product_price=80,
        shipping=10,
        cache_dirs=[tmp_path],
        soft_match=False,
    )
    assert decision.drop is False
    assert decision.detail["no_market_cache"] is True
    assert decision.detail["pricing_basis"] == "SAFE_15_NO_MARKET"


def test_floor_over_market_lock_drops_when_safe_exceeds_band(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "true")
    title = "Outdoor Garden Fountain Waterfall Feature"
    search = extract_search_keywords(title)
    safe = safe_15_price(80, 10)
    assert safe and safe > 0
    median = safe / FLOOR_OVER_MARKET_LOCK - 1  # force above band
    cache_dir = tmp_path / "browse_market_intel"
    cache_dir.mkdir()
    path = browse_cache_path(cache_dir, search)
    path.write_text(
        json.dumps(
            {
                "cached_at": time.time(),
                "data": {
                    "top_keywords": ["outdoor", "garden", "fountain", "waterfall"],
                    "price_stats": {"median": median, "avg": median},
                    "total_listings": 20,
                    "pricing_basis": "BROWSE",
                },
            }
        ),
        encoding="utf-8",
    )
    decision = evaluate_pre_heart_price(
        title=title,
        product_price=80,
        shipping=10,
        cache_dirs=[cache_dir],
        soft_match=False,
    )
    assert decision.drop is True
    assert decision.reason == REASON_FLOOR_OVER_MARKET_LOCK
    assert decision.detail["safe_15_price"] > decision.detail["near_market_ceiling"]


def test_floor_over_market_is_advisory_when_market_gate_off(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "false")
    title = "Outdoor Garden Fountain Waterfall Feature"
    search = extract_search_keywords(title)
    safe = safe_15_price(80, 10)
    median = safe / FLOOR_OVER_MARKET_LOCK - 1
    cache_dir = tmp_path / "browse_market_intel"
    cache_dir.mkdir()
    browse_cache_path(cache_dir, search).write_text(
        json.dumps(
            {
                "cached_at": time.time(),
                "data": {
                    "top_keywords": ["outdoor", "garden", "fountain", "waterfall"],
                    "price_stats": {"median": median, "avg": median},
                    "pricing_basis": "BROWSE",
                },
            }
        ),
        encoding="utf-8",
    )
    decision = evaluate_pre_heart_price(
        title=title,
        product_price=80,
        shipping=10,
        cache_dirs=[cache_dir],
        soft_match=False,
    )
    assert decision.drop is False
    assert decision.reason == ""
    assert decision.detail.get("floor_over_market_advisory") is True
    assert decision.detail.get("far_above_market_advisory") is True


def test_within_band_keeps(tmp_path: Path):
    title = "Cast Iron Skillet Set Pre Seasoned"
    search = extract_search_keywords(title)
    safe = safe_15_price(40, 8)
    median = safe  # SAFE_15 == median → ratio 1.0 < 1.30
    cache_dir = tmp_path / "browse"
    cache_dir.mkdir()
    browse_cache_path(cache_dir, search).write_text(
        json.dumps(
            {
                "cached_at": time.time(),
                "data": {"price_stats": {"median": median, "avg": median}, "top_keywords": ["cast", "iron", "skillet"]},
            }
        ),
        encoding="utf-8",
    )
    decision = evaluate_pre_heart_price(
        title=title,
        product_price=40,
        shipping=8,
        cache_dirs=[cache_dir],
        soft_match=False,
    )
    assert decision.drop is False
    assert decision.detail["no_market_cache"] is False


def test_keyword_exact_cache_hit(tmp_path: Path):
    cache_dir = tmp_path / "browse"
    cache_dir.mkdir()
    kw = "outdoor fountain"
    kw_norm = extract_search_keywords(kw) or kw
    browse_cache_path(cache_dir, kw_norm).write_text(
        json.dumps(
            {
                "cached_at": time.time(),
                "data": {"price_stats": {"median": 50.0, "avg": 50.0}, "top_keywords": ["fountain"]},
            }
        ),
        encoding="utf-8",
    )
    hit = lookup_cached_market_median(
        "Totally Unrelated Product Title XYZ",
        keyword=kw,
        cache_dirs=[cache_dir],
        soft_match=False,
    )
    assert hit["median"] == 50.0
    assert hit["source"] == "keyword_exact"


def test_soft_top_keyword_overlap(tmp_path: Path):
    cache_dir = tmp_path / "browse"
    cache_dir.mkdir()
    # Wrong hash path — only soft match can find it.
    (cache_dir / "deadbeef.json").write_text(
        json.dumps(
            {
                "cached_at": time.time(),
                "data": {
                    "top_keywords": ["jump", "starter", "battery", "booster", "car"],
                    "price_stats": {"median": 60.0, "avg": 60.0},
                },
            }
        ),
        encoding="utf-8",
    )
    hit = lookup_cached_market_median(
        "12V Car Jump Starter Battery Booster Pack",
        cache_dirs=[cache_dir],
        soft_match=True,
        min_soft_overlap=2,
    )
    assert hit["median"] == 60.0
    assert hit["source"] == "soft_top_keywords"
    assert hit["overlap"] >= 2


def test_shipping_missing_without_fetch_keeps_unknown_no_default_zero():
    """Missing freight must not become shipping=0 (that false-kept far-above SKUs)."""
    decision = evaluate_pre_heart_price(
        title="No Cache Widget",
        product_price=50,
        shipping=None,
        sku="TESTSKU001",
        cache_dirs=[Path("/nonexistent-cache-dir")],
        soft_match=False,
        fetch_shipping=False,
    )
    assert decision.drop is False
    assert decision.detail.get("shipping_unknown") is True
    assert decision.detail.get("pricing_basis") == "SHIPPING_UNKNOWN"
    assert "shipping" not in decision.detail or decision.detail.get("shipping") is None
    assert decision.detail.get("safe_15_price") is None


def test_shipping_lookup_used_before_market_drop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "true")
    title = "Outdoor Garden Fountain Waterfall Feature"
    search = extract_search_keywords(title)
    # With ship=0, SAFE_15 understates and would KEEP; with real ship, DROP.
    safe_no_ship = safe_15_price(80, 0)
    safe_with_ship = safe_15_price(80, 25)
    assert safe_no_ship and safe_with_ship and safe_with_ship > safe_no_ship
    median = safe_with_ship / FLOOR_OVER_MARKET_LOCK - 1  # with-ship exceeds band
    # without ship, safe_no_ship may still be <= ceiling
    ceiling = median * FLOOR_OVER_MARKET_LOCK
    assert safe_no_ship <= ceiling < safe_with_ship
    cache_dir = tmp_path / "browse_market_intel"
    cache_dir.mkdir()
    path = browse_cache_path(cache_dir, search)
    path.write_text(
        json.dumps(
            {
                "cached_at": time.time(),
                "data": {
                    "top_keywords": ["outdoor", "garden", "fountain", "waterfall"],
                    "price_stats": {"median": median, "avg": median},
                    "total_listings": 20,
                    "pricing_basis": "BROWSE",
                },
            }
        ),
        encoding="utf-8",
    )
    # Before: missing ship + no fetch → keep unknown (not a false numeric keep)
    before = evaluate_pre_heart_price(
        title=title,
        product_price=80,
        shipping=None,
        sku="FOUNTAIN1",
        cache_dirs=[cache_dir],
        soft_match=False,
        fetch_shipping=False,
    )
    assert before.drop is False
    assert before.detail.get("shipping_unknown") is True
    # After: lookup supplies real ship → drop
    after = evaluate_pre_heart_price(
        title=title,
        product_price=80,
        shipping=None,
        sku="FOUNTAIN1",
        cache_dirs=[cache_dir],
        soft_match=False,
        fetch_shipping=False,
        shipping_lookup={"FOUNTAIN1": 25},
    )
    assert after.drop is True
    assert after.reason == REASON_FLOOR_OVER_MARKET_LOCK
    assert after.detail["shipping"] == 25
    assert after.detail["safe_15_price"] == round(float(safe_with_ship), 2)


def test_filter_batch_shipping_lookup_drops_false_keeps(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "true")
    cache_dir = tmp_path / "browse"
    cache_dir.mkdir()
    title = "Propane Fire Pit Table Outdoor"
    safe_with = safe_15_price(90, 20)
    median = safe_with / FLOOR_OVER_MARKET_LOCK - 5
    browse_cache_path(cache_dir, extract_search_keywords(title)).write_text(
        json.dumps({"cached_at": time.time(), "data": {"price_stats": {"median": median}}}),
        encoding="utf-8",
    )
    items = [
        {"sku": "A1", "title": title, "cargo_usd": 90, "shipping": None, "kw": "fire pit"},
        {"sku": "B2", "title": "Unknown Niche Gizmo", "cargo_usd": 20, "shipping": 2, "kw": "gizmo"},
    ]
    out = filter_pre_heart_items(
        items,
        cache_dirs=[cache_dir],
        soft_match=False,
        fetch_shipping=False,
        shipping_lookup={"A1": 20},
    )
    assert out["stats"]["drop"] == 1
    assert out["drop"][0]["sku"] == "A1"
    assert out["drop"][0]["shipping"] == 20


def test_filter_pre_heart_items_splits_drop_and_keep(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "true")
    cache_dir = tmp_path / "browse"
    cache_dir.mkdir()
    safe = safe_15_price(90, 0)
    median = safe / FLOOR_OVER_MARKET_LOCK - 5
    title = "Propane Fire Pit Table Outdoor"
    browse_cache_path(cache_dir, extract_search_keywords(title)).write_text(
        json.dumps({"cached_at": time.time(), "data": {"price_stats": {"median": median}}}),
        encoding="utf-8",
    )
    items = [
        {"sku": "A1", "title": title, "cargo_usd": 90, "shipping": 0, "kw": "fire pit"},
        {"sku": "B2", "title": "Unknown Niche Gizmo", "cargo_usd": 20, "shipping": 2, "kw": "gizmo"},
    ]
    out = filter_pre_heart_items(items, cache_dirs=[cache_dir], soft_match=False)
    assert out["stats"]["drop"] == 1
    assert out["stats"]["keep"] == 1
    assert out["drop"][0]["sku"] == "A1"
    assert out["drop"][0]["pre_heart_price"]["reason"] == REASON_FLOOR_OVER_MARKET_LOCK
    assert out["keep"][0]["sku"] == "B2"
    assert out["keep"][0]["pre_heart_price"]["detail"]["no_market_cache"] is True


def test_injected_median_skips_disk(monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "true")
    safe = safe_15_price(100, 20)
    decision = evaluate_pre_heart_price(
        title="Anything",
        product_price=100,
        shipping=20,
        market_median=safe / FLOOR_OVER_MARKET_LOCK - 1,
        cache_dirs=[Path("/nope")],
    )
    assert decision.drop is True
    assert decision.reason == REASON_FLOOR_OVER_MARKET_LOCK
