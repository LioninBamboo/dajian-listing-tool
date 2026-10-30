"""P0-3: Browse market-intel 429 backoff, cache, Terapeak fallback, SAFE_15 flag."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qwen_optimizer import QwenOptimizer


@pytest.fixture
def optimizer(tmp_path, monkeypatch):
    opt = QwenOptimizer(api_key="test-key")
    monkeypatch.setattr(opt, "_BROWSE_CACHE_DIR", tmp_path / "browse_cache")
    monkeypatch.setattr(opt, "_extract_search_keywords", lambda title: "oak dining chair")
    return opt


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_browse_429_then_success_uses_backoff_and_browse_basis(optimizer, monkeypatch):
    sleeps = []
    monkeypatch.setattr("qwen_optimizer.time.sleep", lambda s: sleeps.append(s))

    payloads = [
        _Resp(429),
        _Resp(
            200,
            {
                "total": 12,
                "itemSummaries": [
                    {"title": "Oak Dining Chair Set", "price": {"value": "120.00"}, "itemId": "v1|1|0"},
                    {"title": "Solid Oak Chair", "price": {"value": "140.00"}, "itemId": "v1|2|0"},
                ],
            },
        ),
    ]

    def fake_get(*args, **kwargs):
        if "item_summary/search" in args[0]:
            return payloads.pop(0)
        return _Resp(200, {"localizedAspects": []})

    with patch("qwen_optimizer.requests.get", side_effect=fake_get), patch(
        "src.services.ebay_auth.EbayOAuthService"
    ) as oauth_cls:
        oauth = oauth_cls.return_value
        oauth.get_application_token.return_value = "app-token"
        oauth.get_valid_token.return_value = "user-token"
        result = optimizer.fetch_market_intelligence("Oak Dining Chair")

    assert sleeps, "expected backoff sleep after 429"
    assert result["pricing_basis"] == "BROWSE"
    assert result["price_stats"]["median"] > 0


def test_browse_cache_hit_skips_network(optimizer, monkeypatch):
    cached = {
        "top_keywords": ["oak"],
        "competitor_titles": ["Oak Chair"],
        "common_aspects": {},
        "price_stats": {"avg": 100.0, "min": 90.0, "max": 110.0, "median": 100.0},
        "total_listings": 5,
        "pricing_basis": "BROWSE",
        "market_source": "browse",
    }
    optimizer._store_browse_cache("oak dining chair", None, cached)
    with patch("qwen_optimizer.requests.get") as get:
        result = optimizer.fetch_market_intelligence("Oak Dining Chair")
        get.assert_not_called()
    assert result["pricing_basis"] == "BROWSE_CACHE"
    assert result["price_stats"]["median"] == 100.0


def test_all_429_falls_back_to_terapeak(optimizer, monkeypatch):
    monkeypatch.setattr("qwen_optimizer.time.sleep", lambda s: None)
    with patch("qwen_optimizer.requests.get", return_value=_Resp(429)), patch(
        "src.services.ebay_auth.EbayOAuthService"
    ) as oauth_cls, patch.object(
        optimizer,
        "_terapeak_price_fallback",
        return_value={
            "top_keywords": [],
            "competitor_titles": [],
            "common_aspects": {},
            "price_stats": {"avg": 88.0, "min": 80.0, "max": 95.0, "median": 88.0},
            "total_listings": 9,
            "pricing_basis": "TERAPEAK_FALLBACK",
            "market_source": "terapeak",
        },
    ) as fb:
        oauth = oauth_cls.return_value
        oauth.get_application_token.return_value = "app-token"
        result = optimizer.fetch_market_intelligence("Oak Dining Chair")
    fb.assert_called_once()
    assert result["pricing_basis"] == "TERAPEAK_FALLBACK"
    assert result["price_stats"]["median"] == 88.0


def test_no_market_marks_safe15_basis(optimizer, monkeypatch):
    monkeypatch.setattr("qwen_optimizer.time.sleep", lambda s: None)
    with patch("qwen_optimizer.requests.get", return_value=_Resp(200, {"total": 0, "itemSummaries": []})), patch(
        "src.services.ebay_auth.EbayOAuthService"
    ) as oauth_cls, patch.object(optimizer, "_terapeak_price_fallback", return_value={}):
        oauth = oauth_cls.return_value
        oauth.get_application_token.return_value = "app-token"
        result = optimizer.fetch_market_intelligence("Oak Dining Chair")
    assert result["pricing_basis"] == "SAFE_15_NO_MARKET"
    assert not result.get("price_stats")


def test_batch_publish_safe15_no_market_flag(monkeypatch):
    import batch_publish as bp

    product = {
        "sku": "TESTSKU",
        "price": 50.0,
        "shipping": 10.0,
        "suggested_price": 0,
        "attributes": {},
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 70.0},
    }

    class _PE:
        @staticmethod
        def calculate_selling_price(cost, margin):
            return {"selling_price": round(float(cost) * (1 + margin) + 5, 2)}

        @staticmethod
        def calculate_dajian_cost(price, shipping, is_oversize=False):
            return {"total_dajian_cost": float(price) + float(shipping)}

    monkeypatch.setattr(bp, "PricingEngine", _PE)
    # skip live supplier refresh
    monkeypatch.setattr(bp, "DaJianClient", MagicMock(), raising=False)
    monkeypatch.setenv("DAJIAN_API_KEY", "")
    monkeypatch.setenv("DAJIAN_API_SECRET", "")

    final = bp.calculate_smart_final_price(product, market_price=None)
    assert final > 0
    assert product["pricing_basis"] == "SAFE_15_NO_MARKET"
    assert product["market_price"] is None
