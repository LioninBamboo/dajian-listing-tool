"""
统一定价死线测试 (2026-05).

覆盖目标:
1. PricingEngine.absolute_floor_price / safe_floor_price / assert_safe_price 公式正确
2. 所有改价入口最终都遵守死线 (任何价格 >= absolute_floor 才不亏)
3. IntelligenceService.calculate_smart_price 已委托到 PricingEngine (修复历史 bug)
4. scripts/batch_smart_reprice.smart_price 已委托到 PricingEngine (去重复)
5. InventorySyncService.update_ebay_price 守门员能拦下不安全价

红线 (禁止破坏):
- 任意 cost > 0 的 SKU, listing_price < absolute_floor_price(cost) 必须被拒绝.
- IntelligenceService 与 batch_smart_reprice 与 PricingEngine 三者对同样输入返回同样的 final_price.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.pricing_engine import PricingEngine


# ─────────────────────────────────────────────────────────────────────
# 1. 死线公式正确性
# ─────────────────────────────────────────────────────────────────────
class AbsoluteFloorTests(unittest.TestCase):
    """死线 = (cost + 0.30) / ((1-0.05) × (1-0.1325-0.05))"""

    def test_floor_formula_matches_break_even_definition(self):
        # cost = 100 → 净收必须 = 100
        # listing × 0.95 × 0.8175 - 0.30 = 100  →  listing = 100.30 / 0.776625 ≈ 129.16
        cost = 100.0
        floor = PricingEngine.absolute_floor_price(cost)
        # 反向验证: 在 floor 价位上利润恰好 ≈ 0
        net = floor * 0.95 * (1 - 0.1325 - 0.05) - 0.30
        self.assertAlmostEqual(net, cost, places=1)
        self.assertAlmostEqual(floor, 129.16, places=1)

    def test_floor_zero_cost_returns_zero(self):
        self.assertEqual(PricingEngine.absolute_floor_price(0), 0.0)
        self.assertEqual(PricingEngine.absolute_floor_price(-5), 0.0)
        self.assertEqual(PricingEngine.absolute_floor_price(None), 0.0)  # type: ignore[arg-type]

    def test_safe_floor_with_5pct_safety_matches_calculate_smart_price_anti_loss(self):
        # 验证 safe_floor_price(cost, 0.05) == calculate_smart_price 内的 anti_loss_floor
        cost = 100.0
        safe = PricingEngine.safe_floor_price(cost, 0.05)
        # 内联公式 (cost × 1.05 + 0.30) / 0.776625
        expected = (Decimal("100") * Decimal("1.05") + Decimal("0.30")) / (
            Decimal("0.95") * (Decimal("1") - Decimal("0.1825"))
        )
        self.assertAlmostEqual(safe, float(expected), places=2)

    def test_safe_floor_safety_margin_zero_equals_absolute(self):
        cost = 250.0
        self.assertAlmostEqual(
            PricingEngine.safe_floor_price(cost, 0.0),
            PricingEngine.absolute_floor_price(cost),
            places=2,
        )

    def test_safe_floor_negative_safety_raises(self):
        with self.assertRaises(ValueError):
            PricingEngine.safe_floor_price(100, -0.01)


# ─────────────────────────────────────────────────────────────────────
# 2. 守门员行为
# ─────────────────────────────────────────────────────────────────────
class AssertSafePriceTests(unittest.TestCase):

    def test_blocks_below_break_even(self):
        cost = 100.0
        floor = PricingEngine.absolute_floor_price(cost)
        # 比死线低 1 美元 → 应该拒绝
        ok, reason = PricingEngine.assert_safe_price(floor - 1.0, cost)
        self.assertFalse(ok)
        self.assertIn("below", reason)

    def test_allows_at_break_even(self):
        cost = 100.0
        floor = PricingEngine.absolute_floor_price(cost)
        ok, _ = PricingEngine.assert_safe_price(floor, cost)
        self.assertTrue(ok)

    def test_allows_within_tolerance(self):
        cost = 100.0
        floor = PricingEngine.absolute_floor_price(cost)
        # 比死线低 0.005 → 在 tolerance 容差内, 应允许
        ok, _ = PricingEngine.assert_safe_price(floor - 0.005, cost)
        self.assertTrue(ok)

    def test_safety_margin_5pct_blocks_break_even_price(self):
        cost = 100.0
        break_even = PricingEngine.absolute_floor_price(cost)
        # 启用 5% 安全缓冲后, 死线价就不够了
        ok, reason = PricingEngine.assert_safe_price(break_even, cost, safety_margin=0.05)
        self.assertFalse(ok)
        self.assertIn("safe floor", reason.lower())

    def test_no_cost_data_does_not_block(self):
        ok, _ = PricingEngine.assert_safe_price(100.0, total_cost=0)
        self.assertTrue(ok)
        ok, _ = PricingEngine.assert_safe_price(100.0, total_cost=None)  # type: ignore[arg-type]
        self.assertTrue(ok)

    def test_invalid_price_rejected(self):
        ok, _ = PricingEngine.assert_safe_price(0, 100)
        self.assertFalse(ok)
        ok, _ = PricingEngine.assert_safe_price(-5, 100)
        self.assertFalse(ok)


# ─────────────────────────────────────────────────────────────────────
# 3. calculate_smart_price 三个入口对齐
# ─────────────────────────────────────────────────────────────────────
class SmartPriceUnificationTests(unittest.TestCase):
    """三个曾经各自实现的 calculate_smart_price 现在必须返回相同的 final_price."""

    def _load_batch_smart_reprice(self):
        spec = importlib.util.spec_from_file_location(
            "batch_smart_reprice",
            ROOT / "scripts" / "batch_smart_reprice.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_intelligence_service_delegates_to_pricing_engine(self):
        # MI 历史 bug: cost*(1+m) 忽略 fees → 被修复后, MI 与 PE 应一致
        from src.plugins.terapeak_research.intelligence_service import IntelligenceService

        mi = IntelligenceService.__new__(IntelligenceService)  # 跳过 __init__ (Terapeak 依赖)
        mi_result = IntelligenceService.calculate_smart_price(
            mi, total_cost=100.0, market_price=180.0, min_margin=0.10, max_margin=0.35
        )
        pe_result = PricingEngine.calculate_smart_price(
            total_cost=100.0, market_price=180.0, min_margin=0.10, max_margin=0.35
        )
        self.assertAlmostEqual(mi_result["final_price"], pe_result["final_price"], places=2)
        self.assertEqual(mi_result["strategy"], pe_result["strategy"])

    def test_batch_smart_reprice_delegates_to_pricing_engine(self):
        bsr = self._load_batch_smart_reprice()
        bsr_result = bsr.smart_price(total_cost=100.0, market_avg=180.0, min_margin=0.10, max_margin=0.35)
        pe_result = PricingEngine.calculate_smart_price(
            total_cost=100.0, market_price=180.0, min_margin=0.10, max_margin=0.35
        )
        self.assertAlmostEqual(bsr_result["listing_price"], pe_result["final_price"], places=2)
        self.assertEqual(bsr_result["strategy"], pe_result["strategy"])

    def test_smart_price_never_below_break_even(self):
        """对一组随机 (cost, market) 组合, 三个入口给出的 final_price 都必须 >= 死线."""
        scenarios = [
            (100.0, 50.0),    # 市场极低, 应触发 ANTI_LOSS
            (100.0, 130.0),   # 市场刚好接近死线
            (100.0, 200.0),   # 健康市场
            (100.0, 0),       # 无市场数据
            (50.0, 80.0),     # 低成本
            (300.0, 400.0),   # 高成本
        ]
        bsr = self._load_batch_smart_reprice()
        for cost, market in scenarios:
            with self.subTest(cost=cost, market=market):
                floor = PricingEngine.absolute_floor_price(cost)
                pe = PricingEngine.calculate_smart_price(cost, market)["final_price"]
                bsr_p = bsr.smart_price(cost, market)["listing_price"]
                self.assertGreaterEqual(pe + 0.01, floor, f"PE underprices at cost={cost} market={market}")
                self.assertGreaterEqual(bsr_p + 0.01, floor, f"BSR underprices at cost={cost} market={market}")


# ─────────────────────────────────────────────────────────────────────
# 4. InventorySyncService.update_ebay_price 守门员拦截不安全价
# ─────────────────────────────────────────────────────────────────────
class UpdateEbayPriceGuardTests(unittest.TestCase):

    def _make_service(self):
        from src.plugins.inventory_sync.sync_service import InventorySyncService
        svc = InventorySyncService.__new__(InventorySyncService)
        svc.logger = MagicMock()
        svc.db_path = "ebay_collection.db"
        return svc

    def test_blocks_below_break_even_without_calling_ebay(self):
        svc = self._make_service()
        floor = PricingEngine.absolute_floor_price(100.0)
        unsafe = floor - 20.0  # 明显低于关广告死线 → 真亏本

        # mock _fetch_cost_and_listing → cost=100 listing=L1
        with patch("src.services.repricing_guard._fetch_cost_and_listing",
                   return_value=(100.0, "L1")), \
             patch("src.services.ebay_auth.EbayOAuthService") as mock_oauth:
            ok = svc.update_ebay_price("LOSSY-SKU", unsafe)

        self.assertFalse(ok)
        mock_oauth.assert_not_called()  # eBay API 完全没被触达

    def test_passthrough_when_cost_unknown(self):
        # cost 缺失时不应该阻塞 (fail-open, 让原流程继续)
        svc = self._make_service()
        with patch("src.services.repricing_guard._fetch_cost_and_listing",
                   return_value=(None, None)), \
             patch("requests.get") as mock_get, \
             patch("src.services.ebay_auth.EbayOAuthService"):
            mock_get.return_value = MagicMock(status_code=500)
            ok = svc.update_ebay_price("UNKNOWN-COST", 50.0)
        self.assertFalse(ok)  # 因 eBay 500, 不是因守门员
        # 关键: requests.get 被调用了 → 守门员放行了
        mock_get.assert_called()

    def test_allows_safe_price(self):
        svc = self._make_service()
        safe = PricingEngine.safe_floor_price(100.0, 0.05)  # 5% 缓冲
        with patch("src.services.repricing_guard._fetch_cost_and_listing",
                   return_value=(100.0, "L1")), \
             patch("requests.get") as mock_get, \
             patch("src.services.ebay_auth.EbayOAuthService"):
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"offers": []}
            )
            ok = svc.update_ebay_price("SAFE-SKU", safe)
        self.assertFalse(ok)  # 因 offers=[] → 失败, 不是守门员
        # 关键: requests.get 被调用了, 说明守门员放行
        mock_get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
