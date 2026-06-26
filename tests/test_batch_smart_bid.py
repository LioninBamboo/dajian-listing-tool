"""分级动态 bid 优化测试 (P3, 2026-05).

覆盖核心纯函数 (无外部 API): classify, desired_bid_for, cap_bid_by_floor, build_plan.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import batch_smart_bid as bsb  # noqa: E402


class ClassifyTests(unittest.TestCase):

    def test_high_with_high_ctr(self):
        self.assertEqual(bsb.classify(impressions=600, ctr=0.05, sold_qty=0), 'high')

    def test_high_with_sales_even_low_ctr(self):
        self.assertEqual(bsb.classify(impressions=600, ctr=0.001, sold_qty=2), 'high')

    def test_low_with_high_imp_no_ctr_no_sales(self):
        self.assertEqual(bsb.classify(impressions=400, ctr=0.005, sold_qty=0), 'low')

    def test_mid_default(self):
        # impression 不足以触发 low 也没到 high
        self.assertEqual(bsb.classify(impressions=100, ctr=0.01, sold_qty=0), 'mid')

    def test_low_blocked_by_sales(self):
        # 即使 impressions 高 + ctr 低, 但有销量 → 不应判 low
        result = bsb.classify(impressions=400, ctr=0.005, sold_qty=1)
        self.assertNotEqual(result, 'low')


class DesiredBidTests(unittest.TestCase):

    def test_high_returns_7(self):
        self.assertEqual(bsb.desired_bid_for('high'), bsb.HIGH_BID)

    def test_mid_returns_5(self):
        self.assertEqual(bsb.desired_bid_for('mid'), bsb.MID_BID)

    def test_low_returns_3(self):
        self.assertEqual(bsb.desired_bid_for('low'), bsb.LOW_BID)


class CapBidByFloorTests(unittest.TestCase):

    def test_high_price_passes_through(self):
        # cost=50, price=200 → margin huge → 任意 bid 都撑得住
        capped = bsb.cap_bid_by_floor(7.0, live_price=200.0, total_cost=50.0)
        self.assertAlmostEqual(capped, 7.0)

    def test_low_margin_caps_below_desired(self):
        # cost=100, price=130 → 撑不起 7%
        capped = bsb.cap_bid_by_floor(7.0, live_price=130.0, total_cost=100.0)
        self.assertLess(capped, 7.0)

    def test_unsafe_returns_negative(self):
        # 现价 < 关广告死线 → 即使 0% 广告都亏
        capped = bsb.cap_bid_by_floor(5.0, live_price=110.0, total_cost=110.0)
        self.assertLess(capped, 0)

    def test_missing_data_returns_desired(self):
        # 没 price 或 cost → 不动 (返回 desired)
        self.assertEqual(bsb.cap_bid_by_floor(5.0, 0.0, 100.0), 5.0)
        self.assertEqual(bsb.cap_bid_by_floor(5.0, 100.0, 0.0), 5.0)


class BuildPlanTests(unittest.TestCase):

    def test_skips_non_promoted_listings(self):
        products = [{'sku': 'A', 'listing_id': 'L1', 'impressions': 1000, 'ctr': 0.05, 'sold_qty': 1}]
        plan = bsb.build_plan(products, ads_by_listing={}, cost_map={})
        self.assertEqual(plan, [])

    def test_high_tier_promoted_listing_with_safe_price(self):
        products = [{'sku': 'A', 'listing_id': 'L1', 'impressions': 1000, 'ctr': 0.05, 'sold_qty': 2}]
        ads = {'L1': {'campaign_id': 'C1', 'bid_percentage': 5.0}}
        cost_map = {'A': {'total_cost': 50.0, 'listing_id': 'L1'}}

        # 模拟 real_client 返回 live_price=200
        class FakeClient:
            def get_offer_by_sku(self, sku):
                return {'pricingSummary': {'price': {'value': '200.00'}}}

        plan = bsb.build_plan(products, ads, cost_map, real_client=FakeClient())
        self.assertEqual(len(plan), 1)
        item = plan[0]
        self.assertEqual(item['tier'], 'high')
        self.assertEqual(item['decision'], 'adjust')
        self.assertEqual(item['new_bid'], bsb.HIGH_BID)

    def test_unsafe_price_skips(self):
        products = [{'sku': 'B', 'listing_id': 'L2', 'impressions': 1000, 'ctr': 0.05, 'sold_qty': 2}]
        ads = {'L2': {'campaign_id': 'C1', 'bid_percentage': 5.0}}
        cost_map = {'B': {'total_cost': 110.0, 'listing_id': 'L2'}}

        class FakeClient:
            def get_offer_by_sku(self, sku):
                return {'pricingSummary': {'price': {'value': '110.00'}}}

        plan = bsb.build_plan(products, ads, cost_map, real_client=FakeClient())
        self.assertEqual(plan[0]['decision'], 'skip_unsafe')
        self.assertEqual(plan[0]['new_bid'], plan[0]['current_bid'])

    def test_no_change_when_diff_below_threshold(self):
        products = [{'sku': 'C', 'listing_id': 'L3', 'impressions': 50, 'ctr': 0.01, 'sold_qty': 0}]
        ads = {'L3': {'campaign_id': 'C1', 'bid_percentage': 5.0}}  # 已经是 mid 5%
        cost_map = {'C': {'total_cost': 50.0, 'listing_id': 'L3'}}

        class FakeClient:
            def get_offer_by_sku(self, sku):
                return {'pricingSummary': {'price': {'value': '200.00'}}}

        plan = bsb.build_plan(products, ads, cost_map, real_client=FakeClient())
        self.assertEqual(plan[0]['tier'], 'mid')
        self.assertEqual(plan[0]['decision'], 'no_change')


class SummarizeTests(unittest.TestCase):

    def test_summary_counts(self):
        plan = [
            {'decision': 'adjust', 'tier': 'high'},
            {'decision': 'adjust', 'tier': 'low'},
            {'decision': 'no_change', 'tier': 'mid'},
            {'decision': 'skip_unsafe', 'tier': 'mid'},
        ]
        s = bsb.summarize(plan)
        self.assertEqual(s['total'], 4)
        self.assertEqual(s['by_decision']['adjust'], 2)
        self.assertEqual(s['by_decision']['no_change'], 1)
        self.assertEqual(s['by_decision']['skip_unsafe'], 1)
        self.assertEqual(s['by_tier']['high'], 1)
        self.assertEqual(s['by_tier']['mid'], 2)


class RunWiringTests(unittest.TestCase):

    def test_run_uses_real_client_factory(self):
        fake_perf = {'traffic': {}, 'sales': {'by_listing': {}, 'by_sku': {}}}

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(bsb, '_make_real_client', return_value=object()) as make_client,
                patch.object(bsb, '_fetch_cost_map', return_value={}),
                patch.object(bsb, '_load_active_experiments', return_value=[]),
                patch.object(bsb, 'LOG_DIR', Path(temp_dir)),
                patch('src.services.ebay_ad_service.EbayAdService') as ad_cls,
                patch('src.services.ebay_performance.load_performance_cache', return_value=fake_perf),
                patch('src.services.ebay_performance.EbayPerformanceService') as perf_cls,
                patch('src.services.ebay_performance.save_performance_cache') as save_cache,
            ):
                ad_cls.return_value.fetch_all_ad_data.return_value = {'ads': {}}
                perf_cls.return_value.fetch_all_performance.return_value = fake_perf

                report = bsb.run(apply=False, send_email=False)

        make_client.assert_called_once()
        save_cache.assert_not_called()
        self.assertEqual(report['summary']['total'], 0)


if __name__ == '__main__':
    unittest.main()
