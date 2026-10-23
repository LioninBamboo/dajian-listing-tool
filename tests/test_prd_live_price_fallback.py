"""PR-D D2: batch_smart_bid Inventory offer first, Trading GetItem fallback."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from scripts import batch_smart_bid as bsb


# Real Aqua Inventory-API listing (spec D2 case a).
REAL_AQUA_SKU = "W2913P449891"
REAL_AQUA_ITEM_ID = "188683585596"


def test_real_sku_inventory_offer_wins_no_trading(monkeypatch):
    """aqua:W2913P449891 / 188683585596 — Inventory offer used; Trading not called."""
    trading = MagicMock(side_effect=AssertionError("Trading must not be called"))
    monkeypatch.setattr(
        "src.services.ebay_ad_service._trading_live_price", trading,
    )

    class FakeClient:
        def get_offer_by_sku(self, sku):
            assert sku == REAL_AQUA_SKU
            return {"pricingSummary": {"price": {"value": "129.99"}}}

    price = bsb._fetch_live_price(
        REAL_AQUA_SKU, FakeClient(), listing_id=REAL_AQUA_ITEM_ID,
    )
    assert price == pytest.approx(129.99)
    trading.assert_not_called()


def test_synthetic_trading_fallback_when_no_inventory_offer(monkeypatch):
    """SYNTHETIC Aqua Trading-created listing: no Inventory offer → Trading price."""
    trading = MagicMock(return_value=88.50)
    monkeypatch.setattr(
        "src.services.ebay_ad_service._trading_live_price", trading,
    )

    class FakeClient:
        def get_offer_by_sku(self, sku):
            return None

    price = bsb._fetch_live_price(
        "SYNTHETIC-TRADING-SKU", FakeClient(), listing_id="999000111222",
    )
    assert price == pytest.approx(88.50)
    trading.assert_called_once_with("999000111222")


def test_synthetic_offer_zero_without_listing_id_returns_none(monkeypatch):
    trading = MagicMock()
    monkeypatch.setattr(
        "src.services.ebay_ad_service._trading_live_price", trading,
    )

    class FakeClient:
        def get_offer_by_sku(self, sku):
            return {"pricingSummary": {"price": {"value": "0"}}}

    price = bsb._fetch_live_price("SYNTHETIC-ZERO", FakeClient(), listing_id=None)
    assert price is None
    trading.assert_not_called()


def test_synthetic_trading_raises_logs_warning_returns_none(monkeypatch, caplog):
    def boom(lid):
        raise RuntimeError("trading down")

    monkeypatch.setattr(
        "src.services.ebay_ad_service._trading_live_price", boom,
    )

    class FakeClient:
        def get_offer_by_sku(self, sku):
            return None

    with caplog.at_level(logging.WARNING):
        price = bsb._fetch_live_price(
            "SYNTHETIC-TRADING-ERR", FakeClient(), listing_id="111",
        )
    assert price is None
    assert any("Trading GetItem" in r.message for r in caplog.records)


def test_build_plan_passes_listing_id_to_fetch(monkeypatch):
    seen = {}

    def fake_fetch(sku, real_client, listing_id=None):
        seen["args"] = (sku, listing_id)
        return 200.0

    monkeypatch.setattr(bsb, "_fetch_live_price", fake_fetch)
    products = [{
        "sku": "A", "listing_id": "L1",
        "impressions": 1000, "ctr": 0.05, "sold_qty": 2,
    }]
    ads = {"L1": {"campaign_id": "C1", "bid_percentage": 5.0}}
    cost_map = {"A": {"total_cost": 50.0, "listing_id": "L1"}}

    class FakeClient:
        pass

    plan = bsb.build_plan(products, ads, cost_map, real_client=FakeClient())
    assert seen["args"] == ("A", "L1")
    assert plan[0]["live_price"] == 200.0
