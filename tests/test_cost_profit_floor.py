"""口径 A: 写出 / 开广告 / CRO 加价都守「净利润 >= 成本的 10%」.

费用栈: 5% 店铺折扣 + 13.6% FVF + 1.3% 国际费 + 5% 广告 + $0.40,
刊登按 10% 税垫把费用打在折后货款上, 使目的地税 ≤10% 时净利仍 ≥ 成本 10%。
成本 $100 时:
  带 5% 广告的 10% 成本底 ≈ $148.78
  关广告的 10% 成本底 ≈ $138.99
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.services.pricing_engine import PricingEngine
from src.services.repricing_guard import precheck_price


COST = 100.0


class CostProfitFloorConstantTests(unittest.TestCase):
    def test_constant_is_ten_percent_of_cost(self):
        self.assertAlmostEqual(float(PricingEngine.MIN_NET_MARGIN_ON_COST), 0.10)

    def test_smart_floor_matches_ten_percent_of_cost_with_ads(self):
        floor = PricingEngine.safe_floor_price(
            COST, float(PricingEngine.MIN_NET_MARGIN_ON_COST)
        )
        self.assertAlmostEqual(floor, 148.78, places=1)
        smart = PricingEngine.calculate_smart_price(COST, market_price=50.0)
        self.assertGreaterEqual(smart["final_price"] + 0.01, floor)

    def test_floor_keeps_ten_percent_after_tax_pad_and_international(self):
        from src.services.finance_orders import estimate_line_fees

        floor = PricingEngine.safe_floor_price(COST, 0.10)
        merch = round(floor * (1.0 - float(PricingEngine.STORE_DISCOUNT_RATE)), 2)
        tax = round(merch * float(PricingEngine.LISTING_TAX_PAD), 2)
        fee = estimate_line_fees(merch, ad_rate=0.05, tax=tax, order_line_count=1)
        net = round(merch - COST - fee, 2)
        self.assertGreaterEqual(net + 0.05, COST * 0.10)


class PrecheckHonorsTenPercentOfCostTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "t.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE collected_products ("
            "sku TEXT PRIMARY KEY, cost_breakdown TEXT, listing_id TEXT,"
            " shipping REAL, specs TEXT, attributes TEXT)"
        )
        conn.execute(
            "INSERT INTO collected_products VALUES (?, ?, ?, 0, '{}', '{}')",
            ("SKU-100", json.dumps({"total_dajian_cost": COST}), "LID-100"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_price_at_ten_percent_cost_floor_passes_with_ads(self):
        floor = PricingEngine.safe_floor_price(COST, 0.10)
        ok, reason = precheck_price("SKU-100", floor, db_path=self.db_path)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_break_even_price_is_rejected_even_if_ads_can_be_disabled(self):
        """旧死线 (利润=0) 约 $129; 口径 A 要求关广告后仍留成本的 10%."""
        break_even = PricingEngine.absolute_floor_price(COST)
        ok, reason = precheck_price("SKU-100", break_even, db_path=self.db_path)
        self.assertFalse(ok)
        self.assertIn("unsafe_even_without_ad", reason)

    def test_between_ad_on_and_ad_off_ten_percent_floors_disables_ads(self):
        # $144: 低于带广告 10% 底 (~$148.78), 高于关广告 10% 底 (~$138.99)
        from src.services import repricing_guard

        with patch.object(
            repricing_guard, "_try_disable_ad", return_value=(True, "disabled")
        ):
            ok, reason = repricing_guard.precheck_price(
                "SKU-100", 144.0, db_path=self.db_path
            )
        self.assertTrue(ok)
        self.assertIn("ok_after_ad_disabled", reason)


class CreateAdSafeTenPercentOfCostTests(unittest.TestCase):
    def _make_svc(self):
        from src.services.ebay_ad_service import EbayAdService

        svc = EbayAdService.__new__(EbayAdService)
        svc.create_ad = MagicMock(return_value={"success": True, "ad_id": "NEW"})
        return svc

    def test_floor_listing_allows_five_percent_ads(self):
        svc = self._make_svc()
        floor = PricingEngine.safe_floor_price(COST, 0.10)
        with patch(
            "src.services.repricing_guard._fetch_cost_and_listing",
            return_value=(COST, "L1"),
        ), patch("src.services.ebay_ad_service.requests.get") as mock_get, patch(
            "src.services.ebay_auth.EbayOAuthService"
        ) as mock_oauth:
            mock_oauth.return_value.get_valid_token.return_value = "tok"
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "offers": [{"pricingSummary": {"price": {"value": f"{floor:.2f}"}}}]
            }
            res = svc.create_ad_safe("CAMP", "L1", sku="SKU-A", bid_percentage=5.0)
        self.assertTrue(res["success"])
        svc.create_ad.assert_called_once()

    def test_floor_listing_rejects_seven_percent_ads(self):
        svc = self._make_svc()
        floor = PricingEngine.safe_floor_price(COST, 0.10)
        with patch(
            "src.services.repricing_guard._fetch_cost_and_listing",
            return_value=(COST, "L1"),
        ), patch("src.services.ebay_ad_service.requests.get") as mock_get, patch(
            "src.services.ebay_auth.EbayOAuthService"
        ) as mock_oauth:
            mock_oauth.return_value.get_valid_token.return_value = "tok"
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "offers": [{"pricingSummary": {"price": {"value": f"{floor:.2f}"}}}]
            }
            res = svc.create_ad_safe("CAMP", "L1", sku="SKU-A", bid_percentage=7.0)
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "unsafe")
        svc.create_ad.assert_not_called()


class CroBidCapTenPercentOfCostTests(unittest.TestCase):
    def test_floor_sku_cap_is_about_five_percent_ads(self):
        from src.services.cro_margin_aware_bid import bid_cap_for_sku

        db = Path(tempfile.mkdtemp()) / "e.db"
        conn = sqlite3.connect(str(db))
        floor = PricingEngine.safe_floor_price(COST, 0.10)
        conn.execute(
            "CREATE TABLE products (sku TEXT, ourPrice REAL, total_cost REAL)"
        )
        conn.execute(
            "INSERT INTO products VALUES ('FLOOR', ?, ?)", (floor, COST)
        )
        conn.commit()
        conn.close()
        cap = bid_cap_for_sku("FLOOR", db_path=db)
        self.assertGreaterEqual(cap, 4.5)
        self.assertLessEqual(cap, 5.5)

    def test_under_cost_floor_sku_cannot_raise_ads(self):
        from src.services.cro_margin_aware_bid import bid_cap_for_sku

        db = Path(tempfile.mkdtemp()) / "e.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE products (sku TEXT, ourPrice REAL, total_cost REAL)"
        )
        conn.execute("INSERT INTO products VALUES ('THIN', 100.0, 98.0)")
        conn.commit()
        conn.close()
        cap = bid_cap_for_sku("THIN", db_path=db)
        self.assertLessEqual(cap, 0.0)


if __name__ == "__main__":
    unittest.main()
