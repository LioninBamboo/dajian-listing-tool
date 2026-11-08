"""Live verification must use the price the guard actually wrote.

Daily inventory sync and health auto-fix were marking every guard raise as a
failed write: eBay stored the with-ad floor, then verification compared that
live price to the lower requested target.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.services.pricing_engine import PricingEngine
from src.services.repricing_guard import PriceGuardResult
from src.utils.store_profile import StoreProfile
from tests.test_inventory_sync_price_guard import _product, _row, _service


FLOOR = 249.19


def _raised(requested: float):
    """Truthful stand-in for a successful write that the guard lifted."""

    def update(*_args, **_kwargs):
        return SimpleNamespace(written_price=FLOOR, requested_price=requested)

    def verify(*args, **_kwargs):
        expected = args[2] if len(args) > 2 else _kwargs.get("expected_price", _kwargs.get("expected"))
        ok = abs(float(expected) - FLOOR) <= 0.05
        return ok, FLOOR

    return update, verify


def test_inventory_sync_verifies_and_stores_guard_floor(tmp_path):
    sku = "W46563694"
    live = 400.0
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 66.67, "selling_price": live}, live)],
    )
    update, verify = _raised(203.09)
    service.update_ebay_price = lambda sku, price: writes.append((sku, price)) or update()
    service.verify_ebay_price = lambda sku, expected, tolerance=0.05: verify(None, sku, expected)

    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 66.67, "selling_price": live}),
        68.67,
        0.0,
        dry_run=False,
    )

    assert result == pytest.approx(FLOOR)
    breakdown, suggested = _row(db, sku)
    assert suggested == pytest.approx(FLOOR)
    assert breakdown["selling_price"] == pytest.approx(FLOOR)
    assert writes


def test_health_autofix_accepts_guard_floor(monkeypatch, tmp_path):
    from scripts import sales_health_check as shc

    total_cost = 100.0
    floor = PricingEngine.safe_floor_price(total_cost, 0.10)
    current = floor * 1.40
    market = current / 1.25
    plan = shc.plan_auto_reprice(
        current_price=current,
        total_cost=total_cost,
        market_avg=market,
        mode="overpriced",
    )
    assert plan is not None
    assert plan["new_price"] != pytest.approx(FLOOR)

    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, cost_breakdown TEXT, suggested_price REAL)"
    )
    conn.execute(
        "INSERT INTO collected_products (sku, cost_breakdown, suggested_price) VALUES (?, ?, ?)",
        ("SKU-HEALTH", json.dumps({"selling_price": current, "total_dajian_cost": total_cost}), current),
    )
    conn.commit()
    conn.close()

    seen = {}
    update, verify = _raised(plan["new_price"])

    def wrapped_update(*args, **kwargs):
        seen["requested"] = args[2]
        return update()

    def wrapped_verify(*args, **kwargs):
        seen["expected"] = args[2]
        return verify(*args, **kwargs)

    monkeypatch.setattr(shc.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "src.services.ebay_auth.EbayOAuthService",
        lambda *a, **k: MagicMock(),
    )
    import scripts.batch_smart_reprice as reprice

    monkeypatch.setattr(reprice, "update_ebay_price", wrapped_update)
    monkeypatch.setattr(reprice, "verify_ebay_price_update", wrapped_verify)
    import sys

    scripts_dir = str(Path(shc.__file__).resolve().parents[1] / "scripts")
    sys.path.insert(0, scripts_dir)
    try:
        import batch_smart_reprice as bare

        monkeypatch.setattr(bare, "update_ebay_price", wrapped_update)
        monkeypatch.setattr(bare, "verify_ebay_price_update", wrapped_verify)
    finally:
        if scripts_dir in sys.path:
            sys.path.remove(scripts_dir)

    checker = shc.SalesHealthChecker()
    checker.db_path = db
    checker.results["overpriced"] = [{
        "sku": "SKU-HEALTH",
        "listing_id": "L1",
        "current_price": current,
        "total_cost": total_cost,
        "market_avg": market,
    }]
    checker._auto_fix()

    assert seen["expected"] == pytest.approx(FLOOR)
    assert checker.actions_taken[0]["status"] == "success"
    assert checker.actions_taken[0]["new_price"] == pytest.approx(FLOOR)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT suggested_price, cost_breakdown FROM collected_products WHERE sku = ?",
        ("SKU-HEALTH",),
    ).fetchone()
    conn.close()
    assert row[0] == pytest.approx(FLOOR)
    assert json.loads(row[1])["selling_price"] == pytest.approx(FLOOR)


def test_batch_reprice_verifies_guard_floor(monkeypatch, tmp_path):
    from scripts import batch_smart_reprice as reprice

    product = {
        "sku": "SKU-RAISE",
        "title": "Outdoor Bench Patio Furniture",
        "image_url": "",
        "category_id": "20349",
        "total_cost": 100.0,
        "listing_id": "L-1",
        "cost_breakdown": {"total_dajian_cost": 100.0, "selling_price": 400.0},
        "suggested_price": 400.0,
    }
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, cost_breakdown TEXT, suggested_price REAL)"
    )
    conn.execute(
        "INSERT INTO collected_products (sku, cost_breakdown, suggested_price) VALUES (?, ?, ?)",
        ("SKU-RAISE", json.dumps(product["cost_breakdown"]), 400.0),
    )
    conn.commit()
    conn.close()

    seen = {}
    update, verify = _raised(0)

    def wrapped_update(*args, **kwargs):
        seen["requested"] = args[2]
        return update()

    def wrapped_verify(*args, **kwargs):
        seen["expected"] = args[2]
        return verify(*args, **kwargs)

    monkeypatch.setattr(reprice, "get_published_products", lambda: [product])
    monkeypatch.setattr(reprice, "get_ebay_client", lambda: (MagicMock(), MagicMock()))
    market = MagicMock()
    market.market_mode = "browse_only"
    market.get_market_price_snapshot.return_value = {
        "avg_price": 200.0,
        "median_price": 200.0,
        "min_price": 180.0,
        "max_price": 220.0,
        "total_listings": 5,
        "sample_size": 5,
        "sold_count": 0,
        "source": "BROWSE",
    }
    monkeypatch.setattr(reprice, "TerapeakClient", lambda: market)
    monkeypatch.setattr(reprice, "load_sales_cooldown", lambda: (set(), {"available": True}))
    monkeypatch.setattr(reprice, "read_current_listing_price", lambda *a, **k: 400.0)
    monkeypatch.setattr(reprice, "update_ebay_price", wrapped_update)
    monkeypatch.setattr(reprice, "verify_ebay_price_update", wrapped_verify)
    monkeypatch.setattr(reprice.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(reprice, "PROJECT_ROOT", tmp_path)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)

    report = reprice.run_batch_reprice(dry_run=False, send_email=False)
    row = next(item for item in report["results"] if item["sku"] == "SKU-RAISE")
    assert seen["expected"] == pytest.approx(FLOOR)
    assert row["status"] == "updated"
    assert row["new_price"] == pytest.approx(FLOOR)

    conn = sqlite3.connect(db)
    stored = json.loads(conn.execute(
        "SELECT cost_breakdown FROM collected_products WHERE sku = ?",
        ("SKU-RAISE",),
    ).fetchone()[0])
    conn.close()
    assert stored["selling_price"] == pytest.approx(FLOOR)


def test_update_ebay_price_reports_guard_adjusted_price():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"
    decision = PriceGuardResult(
        ok=True,
        reason="raised_to_with_ad_floor",
        adjusted_price=FLOOR,
        requested_price=203.09,
    )
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {
        "offers": [{
            "offerId": "off-1",
            "status": "PUBLISHED",
            "marketplaceId": "EBAY_US",
            "listing": {"listingId": "123", "listingStatus": "ACTIVE"},
            "pricingSummary": {"price": {"value": "400.00"}},
        }]
    }
    put_resp = MagicMock()
    put_resp.status_code = 204
    put_resp.text = ""

    with patch(
        "src.services.repricing_guard.precheck_price", return_value=decision
    ), patch(
        "src.utils.store_profile.get_store_profile",
        return_value=StoreProfile(),
    ), patch.object(reprice.requests, "get", return_value=get_resp), patch.object(
        reprice.requests, "put", return_value=put_resp
    ) as put:
        ok = reprice.update_ebay_price(oauth, "W46563694", 203.09, expected_listing_id="123")

    body = put.call_args.kwargs["json"]
    assert float(body["pricingSummary"]["price"]["value"]) == pytest.approx(FLOOR)
    assert ok.written_price == pytest.approx(FLOOR)
