"""Regression coverage for MI external (uncollected GigaCloud) discovery.

The user complaint that triggered this slice (2026-05-06):
    "如果每天选出来的产品都是已经刊登的，而且是不变的，那有什么意思呢，
     浪费我的token，给我去扫GIGA上未刊登且有机会的"

These tests pin three contracts of
``src.plugins.terapeak_research.external_discovery``:

1. SKUs in ``PUBLISHED`` / ``READY`` (currently live) are filtered out;
   ``ENDED`` / ``DELISTED`` rows ARE eligible (re-publish opportunities) and
   come back tagged with ``existing_status``.
2. Fresh GigaCloud SKUs are scored via the same
   ``IntelligenceService.analyze_market`` + ``calculate_smart_price`` pipeline
   and returned with ``external=True`` and ``status='UNCOLLECTED'``.
3. ``ingest_external_opportunity_as_pending`` returns a tristate string:
   ``"inserted"`` for new SKUs, ``"reactivated"`` for ENDED/DELISTED rows
   flipped back to PENDING, ``"skipped_live"`` for currently-live rows.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_db(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE collected_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE,
            title TEXT,
            price REAL,
            shipping REAL,
            stock INTEGER,
            url TEXT,
            images TEXT,
            videos TEXT,
            description TEXT,
            attributes TEXT,
            specs TEXT,
            cost_breakdown TEXT,
            suggested_price REAL,
            optimization TEXT,
            status TEXT,
            listing_id TEXT,
            logs TEXT,
            created_at TEXT,
            updated_at TEXT,
            published_at TEXT
        )
        """
    )
    # Seed three rows: PUBLISHED (must be filtered), ENDED (eligible — re-publish
    # opportunity), DELISTED (eligible).
    conn.execute(
        "INSERT INTO collected_products (sku, title, price, status) "
        "VALUES (?, ?, ?, ?)",
        ("LIVE-SKU", "Already Listed Sofa", 199.0, "PUBLISHED"),
    )
    conn.execute(
        "INSERT INTO collected_products (sku, title, price, status) "
        "VALUES (?, ?, ?, ?)",
        ("ENDED-SKU", "Once Listed Chair", 99.0, "ENDED"),
    )
    conn.commit()
    conn.close()
    return str(db_path)


def _make_market(avg_price: float):
    market = MagicMock()
    market.avg_price = avg_price
    market.median_price = avg_price
    market.competition_level = "medium"
    market.price_spread = 0.15
    market.active_total = 1234
    market.demand_signal_score = 60
    market.demand_signal_label = "需求中等"
    return market


def _make_intel(db_path: str):
    intel = MagicMock()
    intel.db_path = db_path
    intel._load_mi_blacklist.return_value = set()
    intel._extract_search_keywords.side_effect = lambda title: title.lower().split()[0] if title else ""
    intel.analyze_market.return_value = _make_market(avg_price=380.0)
    intel.calculate_smart_price.return_value = {
        "final_price": 349.99,
        "margin": 0.25,
        "strategy": "competitive",
    }
    intel._calculate_opportunity_score.return_value = 78
    intel._get_discovery_recommendation.return_value = "✅ 推荐刊登"
    return intel


def _make_dajian_client(records: List[Dict], details: Dict[str, Dict],
                       prices: Dict[str, Dict]):
    client = MagicMock()
    # Single page; signal end-of-data by returning fewer than page_size.
    client.get_product_list_with_page_info.return_value = {
        "records": records,
        "pageInfo": {"total": len(records)},
    }
    client.get_product_details.side_effect = lambda skus: [
        details[s] for s in skus if s in details
    ]
    client.get_product_prices.side_effect = lambda skus: [
        prices[s] for s in skus if s in prices
    ]
    return client


def test_external_discovery_skips_live_keeps_ended_and_fresh(fake_db):
    from src.plugins.terapeak_research.external_discovery import (
        discover_external_opportunities,
    )

    records = [
        {"sku": "LIVE-SKU"},   # PUBLISHED → filtered
        {"sku": "ENDED-SKU"},  # ENDED → eligible (re-publish opportunity)
        {"sku": "FRESH-SKU-A"},
        {"sku": "FRESH-SKU-B"},
    ]
    details = {
        "ENDED-SKU": {
            "sku": "ENDED-SKU", "productName": "Once Listed Chair",
            "imageUrls": [], "weight": 10, "weightUnit": "lbs",
        },
        "FRESH-SKU-A": {
            "sku": "FRESH-SKU-A",
            "productName": "Modern Sectional Sofa",
            "imageUrls": ["https://img.example/a.jpg"],
            "weight": 50, "weightUnit": "lbs",
        },
        "FRESH-SKU-B": {
            "sku": "FRESH-SKU-B",
            "productName": "Compact Coffee Table",
            "imageUrls": [],
            "weight": 20, "weightUnit": "lbs",
        },
    }
    prices = {
        "ENDED-SKU": {"sku": "ENDED-SKU", "price": 40.0, "shippingFee": 10.0},
        "FRESH-SKU-A": {"sku": "FRESH-SKU-A", "price": 120.0, "shippingFee": 30.0},
        "FRESH-SKU-B": {"sku": "FRESH-SKU-B", "price": 60.0,
                        "shippingFeeRange": {"minAmount": 10, "maxAmount": 20}},
    }
    client = _make_dajian_client(records, details, prices)
    intel = _make_intel(fake_db)

    opps, diag = discover_external_opportunities(
        intel,
        dajian_client=client,
        min_margin=0.20,
        max_results=10,
        max_candidates=10,
        page_size=100,
        max_pages=1,
        db_path=fake_db,
        return_diagnostics=True,
    )

    skus = {o["sku"] for o in opps}
    assert "LIVE-SKU" not in skus, "PUBLISHED SKUs must be filtered out"
    assert skus == {"ENDED-SKU", "FRESH-SKU-A", "FRESH-SKU-B"}
    by_sku = {o["sku"]: o for o in opps}
    assert by_sku["ENDED-SKU"]["existing_status"] == "ENDED"
    assert by_sku["ENDED-SKU"]["status"] == "ENDED"
    assert by_sku["FRESH-SKU-A"]["existing_status"] == ""
    assert by_sku["FRESH-SKU-A"]["status"] == "UNCOLLECTED"
    for opp in opps:
        assert opp["external"] is True
        assert opp["opportunity_score"] == 78
        assert opp["margin_rate"] == 25.0
    # Diagnostics sanity
    assert diag["scanned"] == 4
    assert diag["skipped_live"] == 1
    assert diag["candidates"] == 3
    assert diag["kept"] == 3


def test_external_discovery_filters_zero_market_and_low_margin(fake_db):
    from src.plugins.terapeak_research.external_discovery import (
        discover_external_opportunities,
    )

    records = [{"sku": "FRESH-NOMARKET"}, {"sku": "FRESH-LOWMARGIN"}]
    details = {
        "FRESH-NOMARKET": {"sku": "FRESH-NOMARKET", "productName": "Obscure Item",
                           "imageUrls": [], "weight": 5, "weightUnit": "lbs"},
        "FRESH-LOWMARGIN": {"sku": "FRESH-LOWMARGIN", "productName": "Tight Margin Chair",
                            "imageUrls": [], "weight": 15, "weightUnit": "lbs"},
    }
    prices = {
        "FRESH-NOMARKET": {"sku": "FRESH-NOMARKET", "price": 100, "shippingFee": 20},
        "FRESH-LOWMARGIN": {"sku": "FRESH-LOWMARGIN", "price": 100, "shippingFee": 20},
    }
    client = _make_dajian_client(records, details, prices)

    intel = _make_intel(fake_db)
    # First SKU: market analysis returns avg_price 0 → filtered.
    # Second SKU: market valid but margin below threshold → filtered.
    intel.analyze_market.side_effect = [
        _make_market(avg_price=0.0),
        _make_market(avg_price=130.0),
    ]
    intel.calculate_smart_price.return_value = {
        "final_price": 119.99, "margin": 0.05, "strategy": "competitive",
    }

    opps = discover_external_opportunities(
        intel,
        dajian_client=client,
        min_margin=0.20,
        max_results=10,
        max_candidates=10,
        page_size=100,
        max_pages=1,
        db_path=fake_db,
    )
    assert opps == []


def test_ingest_external_opportunity_tristate(fake_db):
    from src.plugins.terapeak_research.external_discovery import (
        ingest_external_opportunity_as_pending,
    )

    # 1) Brand-new SKU → inserted
    new_opp = {
        "sku": "FRESH-INGEST-1",
        "title": "Brand New Bookcase",
        "product_price": 80.0,
        "shipping_cost": 25.0,
        "suggested_price": 199.99,
        "cost_breakdown": {"total_dajian_cost": 110.5},
        "images": ["https://img.example/x.jpg"],
        "opportunity_score": 82,
        "margin_rate": 28.5,
    }
    assert ingest_external_opportunity_as_pending(new_opp, db_path=fake_db) == "inserted"
    # Second call → the row now exists in PENDING (non-live), so it gets
    # "reactivated" (idempotent re-tag) rather than duplicated.
    assert ingest_external_opportunity_as_pending(new_opp, db_path=fake_db) == "reactivated"

    # 2) ENDED row in fixture → reactivated
    ended_opp = {
        "sku": "ENDED-SKU",
        "title": "Once Listed Chair",
        "product_price": 40.0,
        "shipping_cost": 10.0,
        "suggested_price": 99.99,
        "cost_breakdown": {"total_dajian_cost": 55.0},
        "opportunity_score": 65,
        "margin_rate": 22.0,
    }
    assert ingest_external_opportunity_as_pending(ended_opp, db_path=fake_db) == "reactivated"

    # 3) Currently live SKU → skipped
    live_opp = {
        "sku": "LIVE-SKU",
        "title": "Already Listed Sofa",
        "product_price": 100.0,
        "shipping_cost": 20.0,
        "suggested_price": 250.0,
        "cost_breakdown": {"total_dajian_cost": 130.0},
        "opportunity_score": 50,
        "margin_rate": 18.0,
    }
    assert ingest_external_opportunity_as_pending(live_opp, db_path=fake_db) == "skipped_live"

    conn = sqlite3.connect(fake_db)
    try:
        rows = dict(conn.execute(
            "SELECT sku, status FROM collected_products"
        ).fetchall())
    finally:
        conn.close()
    assert rows["FRESH-INGEST-1"] == "PENDING"
    assert rows["ENDED-SKU"] == "PENDING"  # flipped back
    assert rows["LIVE-SKU"] == "PUBLISHED"  # untouched
