"""Price guard must fail closed on exceptions (no eBay write).

Live write points that previously logged '放行' and continued on guard
exceptions now skip the SKU. batch_smart_reprice records status=guard_error.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.services.pricing_engine import PricingEngine
from src.utils.store_profile import StoreProfile


# ── batch_smart_reprice.update_ebay_price ─────────────────────────────────


def test_batch_reprice_guard_exception_returns_guard_error_no_write():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"

    with patch(
        "src.services.repricing_guard.precheck_price",
        side_effect=RuntimeError("db locked"),
    ), patch(
        "src.utils.store_profile.get_store_profile", return_value=StoreProfile()
    ), patch.object(reprice.requests, "get") as get, patch.object(
        reprice.requests, "put"
    ) as put, patch.object(reprice.requests, "post") as post:
        ok = reprice.update_ebay_price(
            oauth, "SKU-GERR", 150.0, expected_listing_id="L1"
        )

    assert ok == reprice.PRICE_GUARD_ERROR
    assert ok == "guard_error"
    assert bool(ok) is False
    get.assert_not_called()
    put.assert_not_called()
    post.assert_not_called()
    oauth.get_valid_token.assert_not_called()


def test_batch_reprice_guard_refuse_still_false_no_write():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"

    with patch(
        "src.services.repricing_guard.precheck_price",
        return_value=(False, "unsafe_even_without_ad: floor"),
    ), patch(
        "src.utils.store_profile.get_store_profile", return_value=StoreProfile()
    ), patch.object(reprice.requests, "get") as get, patch.object(
        reprice.requests, "put"
    ) as put, patch.object(reprice.requests, "post") as post:
        ok = reprice.update_ebay_price(
            oauth, "SKU-REFUSE", 50.0, expected_listing_id="L1"
        )

    assert ok is False
    assert ok != reprice.PRICE_GUARD_ERROR
    get.assert_not_called()
    put.assert_not_called()
    post.assert_not_called()


def test_batch_reprice_guard_pass_still_writes_inventory():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {
        "offers": [
            {
                "offerId": "off-1",
                "status": "PUBLISHED",
                "marketplaceId": "EBAY_US",
                "listing": {"listingId": "123", "listingStatus": "ACTIVE"},
                "pricingSummary": {"price": {"value": "10.00"}},
            }
        ]
    }
    put_resp = MagicMock()
    put_resp.status_code = 204
    put_resp.text = ""

    with patch(
        "src.services.repricing_guard.precheck_price", return_value=(True, "ok")
    ), patch(
        "src.utils.store_profile.get_store_profile", return_value=StoreProfile()
    ), patch.object(reprice.requests, "get", return_value=get_resp) as get, patch.object(
        reprice.requests, "put", return_value=put_resp
    ) as put, patch.object(reprice.requests, "post") as post:
        ok = reprice.update_ebay_price(
            oauth, "SKU-OK", 12.34, expected_listing_id="123"
        )

    assert ok is True
    get.assert_called()
    put.assert_called()
    post.assert_not_called()


def test_run_batch_reprice_reports_guard_error(monkeypatch, tmp_path):
    """Apply path: guard exception → result status guard_error + summary count."""
    from scripts import batch_smart_reprice as reprice

    product = {
        "sku": "SKU-GERR",
        "title": "Outdoor Bench Patio Furniture",
        "image_url": "",
        "category_id": "20349",
        "total_cost": 100.0,
        "listing_id": "L-1",
        "cost_breakdown": {"total_dajian_cost": 100.0, "selling_price": 180.0},
        "suggested_price": 180.0,
    }
    client = MagicMock()
    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"

    market_client = MagicMock()
    market_client.market_mode = "browse_only"
    market_client.get_market_price_snapshot.return_value = {
        "avg_price": 200.0,
        "median_price": 200.0,
        "min_price": 180.0,
        "max_price": 220.0,
        "total_listings": 5,
        "sample_size": 5,
        "sold_count": 0,
        "source": "BROWSE",
    }

    monkeypatch.setattr(reprice, "get_published_products", lambda: [product])
    monkeypatch.setattr(reprice, "get_ebay_client", lambda: (client, oauth))
    monkeypatch.setattr(reprice, "TerapeakClient", lambda: market_client)
    monkeypatch.setattr(reprice, "load_sales_cooldown", lambda: (set(), {"available": True}))
    monkeypatch.setattr(reprice, "read_current_listing_price", lambda *a, **k: 180.0)
    monkeypatch.setattr(
        reprice, "update_ebay_price", lambda *a, **k: reprice.PRICE_GUARD_ERROR
    )
    monkeypatch.setattr(reprice.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.chdir(tmp_path)
    # Reports write under PROJECT_ROOT/reports; redirect via patch.
    monkeypatch.setattr(reprice, "PROJECT_ROOT", tmp_path)
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)

    report = reprice.run_batch_reprice(dry_run=False, send_email=False)

    assert report["summary"]["guard_error"] == 1
    assert report["summary"]["errors"] >= 1
    rows = [r for r in report["results"] if r["sku"] == "SKU-GERR"]
    assert len(rows) == 1
    assert rows[0]["status"] == "guard_error"
    assert "guard" in (rows[0].get("error") or "").lower()


# ── InventorySyncService.update_ebay_price ────────────────────────────────


class TestInventorySyncGuardFailClosed:
    def _make_service(self):
        from src.plugins.inventory_sync.sync_service import InventorySyncService

        svc = InventorySyncService.__new__(InventorySyncService)
        svc.logger = MagicMock()
        svc.db_path = "ebay_collection.db"
        return svc

    def test_guard_exception_blocks_ebay_write(self):
        svc = self._make_service()
        with patch(
            "src.services.repricing_guard.precheck_price",
            side_effect=RuntimeError("boom"),
        ), patch("src.services.ebay_auth.EbayOAuthService") as mock_oauth, patch(
            "requests.get"
        ) as mock_get:
            ok = svc.update_ebay_price("SKU-GERR", 150.0)

        assert ok is False
        mock_oauth.assert_not_called()
        mock_get.assert_not_called()

    def test_guard_refuse_still_blocks(self):
        svc = self._make_service()
        floor = PricingEngine.absolute_floor_price(100.0)
        unsafe = floor - 20.0
        with patch(
            "src.services.repricing_guard._fetch_cost_and_listing",
            return_value=(100.0, "L1"),
        ), patch("src.services.ebay_auth.EbayOAuthService") as mock_oauth:
            ok = svc.update_ebay_price("LOSSY-SKU", unsafe)

        assert ok is False
        mock_oauth.assert_not_called()

    def test_guard_pass_still_reaches_ebay(self):
        svc = self._make_service()
        safe = PricingEngine.safe_floor_price(100.0, 0.10)
        with patch(
            "src.services.repricing_guard._fetch_cost_and_listing",
            return_value=(100.0, "L1"),
        ), patch("requests.get") as mock_get, patch(
            "src.services.ebay_auth.EbayOAuthService"
        ):
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"offers": []}
            )
            ok = svc.update_ebay_price("SAFE-SKU", safe)
        assert ok is False  # empty offers → write path failure, not guard
        mock_get.assert_called_once()
