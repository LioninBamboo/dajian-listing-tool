"""广告自适应死线 + 触底自动关广告测试 (2026-05).

覆盖:
1. 死线函数接受 ad_rate 参数, ad_rate=0 时死线显著低于默认 (5%) 的死线
2. required_ad_rate_for 反算正确
3. precheck_price 三态:
   a. 价格安全 → 通过
   b. 价格 < 带广告死线但 >= 关广告死线, 且 listing 在打广告 → 自动关广告 → 通过
   c. 价格 < 带广告死线, 且 listing 不在打广告 → 通过 (因为本就没付广告费)
   d. 价格 < 带广告死线, 关广告 API 失败 → 拒绝
   e. 价格 < 关广告死线 (真亏本) → 拒绝
4. EbayAdService.find_ad_for_listing / is_listing_promoted 工作正常
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.pricing_engine import PricingEngine


# ─────────────────────────────────────────────────────────────────────
# 1. 死线公式接受 ad_rate
# ─────────────────────────────────────────────────────────────────────
class AdAwareFloorFormulaTests(unittest.TestCase):

    def test_floor_with_ad_off_lower_than_with_ad_on(self):
        cost = 100.0
        floor_with_ad = PricingEngine.absolute_floor_price(cost)              # 默认 5% 广告
        floor_no_ad   = PricingEngine.absolute_floor_price(cost, ad_rate=0.0)  # 关广告
        self.assertLess(floor_no_ad, floor_with_ad)
        # 差距应当显著 (起码 4% 以上)
        self.assertGreater((floor_with_ad - floor_no_ad) / floor_with_ad, 0.04)

    def test_floor_no_ad_value_correct(self):
        # cost=100, 无广告: listing × 0.95 × (1-0.1325) - 0.30 = 100
        # listing = 100.30 / (0.95 × 0.8675) = 100.30 / 0.824125 ≈ 121.71
        floor = PricingEngine.absolute_floor_price(100.0, ad_rate=0.0)
        self.assertAlmostEqual(floor, 121.70, delta=0.05)

    def test_safe_floor_respects_ad_rate(self):
        cost = 50.0
        f1 = PricingEngine.safe_floor_price(cost, safety_margin=0.05, ad_rate=None)
        f2 = PricingEngine.safe_floor_price(cost, safety_margin=0.05, ad_rate=0.0)
        self.assertLess(f2, f1)

    def test_assert_safe_price_with_ad_rate_zero_passes(self):
        # cost=100, price=$122 → 带 5% 广告会拒, 关广告就过
        ok_with_ad, _ = PricingEngine.assert_safe_price(122.0, 100.0)
        ok_no_ad, _   = PricingEngine.assert_safe_price(122.0, 100.0, ad_rate=0.0)
        self.assertFalse(ok_with_ad)
        self.assertTrue(ok_no_ad)

    def test_invalid_ad_rate_rejected(self):
        with self.assertRaises(ValueError):
            PricingEngine.absolute_floor_price(100.0, ad_rate=-0.1)
        with self.assertRaises(ValueError):
            PricingEngine.absolute_floor_price(100.0, ad_rate=0.9)  # ≥ 1 - fvf

    def test_required_ad_rate_for(self):
        # 在带广告死线上, required_ad_rate ≈ 5%
        cost = 100.0
        floor_5 = PricingEngine.absolute_floor_price(cost)  # ~129.16
        ar = PricingEngine.required_ad_rate_for(floor_5, cost)
        self.assertAlmostEqual(ar, 0.05, delta=0.005)

        # 在关广告死线上, required_ad_rate ≈ 0
        floor_0 = PricingEngine.absolute_floor_price(cost, ad_rate=0.0)  # ~121.71
        ar0 = PricingEngine.required_ad_rate_for(floor_0, cost)
        self.assertAlmostEqual(ar0, 0.0, delta=0.005)

        # 价格 < 关广告死线, 应返回负值 (无解)
        ar_neg = PricingEngine.required_ad_rate_for(100.0, cost)
        self.assertLess(ar_neg, 0)


# ─────────────────────────────────────────────────────────────────────
# 2. precheck_price 三态行为
# ─────────────────────────────────────────────────────────────────────
class PrecheckPriceTests(unittest.TestCase):

    def setUp(self):
        # 临时 sqlite db
        import sqlite3, json
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE collected_products (
                sku TEXT PRIMARY KEY,
                cost_breakdown TEXT,
                listing_id TEXT
            )
        """)
        conn.execute(
            "INSERT INTO collected_products VALUES (?, ?, ?)",
            ('SKU-100', json.dumps({'total_dajian_cost': 100.0}), 'LID-100'),
        )
        conn.execute(
            "INSERT INTO collected_products VALUES (?, ?, ?)",
            ('SKU-NOLISTING', json.dumps({'total_dajian_cost': 100.0}), None),
        )
        conn.execute(
            "INSERT INTO collected_products VALUES (?, ?, ?)",
            ('SKU-NOCOST', None, 'LID-NC'),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_safe_price_passes(self):
        from src.services.repricing_guard import precheck_price
        ok, reason = precheck_price('SKU-100', 150.0, db_path=self.db_path)
        self.assertTrue(ok)
        self.assertEqual(reason, 'ok')

    def test_unsafe_below_break_even_rejected(self):
        from src.services.repricing_guard import precheck_price
        # $100 远低于关广告死线 $121.71 → 拒
        ok, reason = precheck_price('SKU-100', 100.0, db_path=self.db_path)
        self.assertFalse(ok)
        self.assertIn('unsafe_even_without_ad', reason)

    def test_no_cost_data_fails_open(self):
        from src.services.repricing_guard import precheck_price
        ok, reason = precheck_price('SKU-NOCOST', 5.0, db_path=self.db_path)
        self.assertTrue(ok)
        self.assertEqual(reason, 'no_cost_data')

    def test_below_ad_floor_no_ad_passes(self):
        """价格 < 带广告死线, 但 listing 没在打广告 → 放行."""
        from src.services import repricing_guard
        with patch.object(repricing_guard, '_try_disable_ad', return_value=(False, 'no_ad')):
            ok, reason = repricing_guard.precheck_price(
                'SKU-100', 122.0, db_path=self.db_path,
            )
        self.assertTrue(ok)
        self.assertEqual(reason, 'ok_no_ad_to_disable')

    def test_below_ad_floor_with_ad_disable_succeeds(self):
        from src.services import repricing_guard
        with patch.object(repricing_guard, '_try_disable_ad', return_value=(True, 'disabled')):
            ok, reason = repricing_guard.precheck_price(
                'SKU-100', 122.0, db_path=self.db_path,
            )
        self.assertTrue(ok)
        self.assertIn('ok_after_ad_disabled', reason)

    def test_below_ad_floor_with_ad_disable_failed_rejected(self):
        from src.services import repricing_guard
        with patch.object(repricing_guard, '_try_disable_ad', return_value=(False, 'failed')):
            ok, reason = repricing_guard.precheck_price(
                'SKU-100', 122.0, db_path=self.db_path,
            )
        self.assertFalse(ok)
        self.assertIn('ad_disable_failed', reason)

    def test_allow_ad_disable_false_rejects_below_ad_floor(self):
        from src.services.repricing_guard import precheck_price
        ok, reason = precheck_price(
            'SKU-100', 122.0, db_path=self.db_path, allow_ad_disable=False,
        )
        self.assertFalse(ok)
        self.assertIn('unsafe_with_ad_no_disable', reason)


# ─────────────────────────────────────────────────────────────────────
# 3. _try_disable_ad 与 EbayAdService 集成
# ─────────────────────────────────────────────────────────────────────
class TryDisableAdTests(unittest.TestCase):

    def test_no_listing_id_returns_unknown(self):
        from src.services.repricing_guard import _try_disable_ad
        ok, status = _try_disable_ad('SKU-X', None, 'reason')
        self.assertFalse(ok)
        self.assertEqual(status, 'unknown')

    def test_no_ad_for_listing_returns_no_ad(self):
        from src.services import repricing_guard
        mock_svc = MagicMock()
        mock_svc.find_ad_for_listing.return_value = None
        with patch.object(repricing_guard, 'EbayAdService', create=True), \
             patch('src.services.ebay_ad_service.EbayAdService', return_value=mock_svc):
            ok, status = repricing_guard._try_disable_ad('SKU-X', 'LID-1', 'reason')
        self.assertFalse(ok)
        self.assertEqual(status, 'no_ad')

    def test_delete_ad_success_returns_disabled(self):
        from src.services import repricing_guard
        mock_svc = MagicMock()
        mock_svc.find_ad_for_listing.return_value = {
            'campaign_id': 'C1', 'ad_id': 'A1', 'bid_percentage': 5.0,
        }
        mock_svc.delete_ad.return_value = {'success': True, 'listing_id': 'LID-1'}
        with patch('src.services.ebay_ad_service.EbayAdService', return_value=mock_svc):
            ok, status = repricing_guard._try_disable_ad('SKU-X', 'LID-1', 'reason')
        self.assertTrue(ok)
        self.assertEqual(status, 'disabled')
        # delete_ad takes (campaign_id, listing_id), NOT ad_id
        mock_svc.delete_ad.assert_called_once_with('C1', 'LID-1')

    def test_delete_ad_failure_returns_failed(self):
        from src.services import repricing_guard
        mock_svc = MagicMock()
        mock_svc.find_ad_for_listing.return_value = {
            'campaign_id': 'C1', 'ad_id': 'A1', 'bid_percentage': 5.0,
        }
        mock_svc.delete_ad.return_value = {'success': False, 'error': 'boom'}
        with patch('src.services.ebay_ad_service.EbayAdService', return_value=mock_svc):
            ok, status = repricing_guard._try_disable_ad('SKU-X', 'LID-1', 'reason')
        self.assertFalse(ok)
        self.assertEqual(status, 'failed')


# ─────────────────────────────────────────────────────────────────────
# 4. EbayAdService.find_ad_for_listing
# ─────────────────────────────────────────────────────────────────────
class FindAdForListingTests(unittest.TestCase):

    def test_find_ad_returns_first_match(self):
        from src.services.ebay_ad_service import EbayAdService
        svc = EbayAdService.__new__(EbayAdService)  # 跳过 __init__
        # Mock 出底层方法
        svc.fetch_campaigns = MagicMock(return_value=[
            {'campaignId': 'C1'}, {'campaignId': 'C2'},
        ])
        svc.fetch_campaign_ads = MagicMock(side_effect=[
            [{'listingId': '111', 'adId': 'A1', 'bidPercentage': '5.0'}],
            [{'listingId': '222', 'adId': 'A2', 'bidPercentage': '6.5'}],
        ])
        # 用全新 _xc 字典 (避免 default mutable 缓存污染)
        result = svc.find_ad_for_listing('222', _xc={})
        self.assertIsNotNone(result)
        self.assertEqual(result['campaign_id'], 'C2')
        self.assertEqual(result['ad_id'], 'A2')
        self.assertAlmostEqual(result['bid_percentage'], 6.5)

    def test_find_ad_returns_none_for_unknown_listing(self):
        from src.services.ebay_ad_service import EbayAdService
        svc = EbayAdService.__new__(EbayAdService)
        svc.fetch_campaigns = MagicMock(return_value=[{'campaignId': 'C1'}])
        svc.fetch_campaign_ads = MagicMock(return_value=[
            {'listingId': '111', 'adId': 'A1', 'bidPercentage': '5.0'},
        ])
        result = svc.find_ad_for_listing('999', _xc={})
        self.assertIsNone(result)

    def test_is_listing_promoted_wrapper(self):
        from src.services.ebay_ad_service import EbayAdService
        svc = EbayAdService.__new__(EbayAdService)
        svc.fetch_campaigns = MagicMock(return_value=[{'campaignId': 'C1'}])
        svc.fetch_campaign_ads = MagicMock(return_value=[
            {'listingId': '111', 'adId': 'A1', 'bidPercentage': '5.0'},
        ])
        # 直接重写 find_ad_for_listing 简化
        svc.find_ad_for_listing = MagicMock(return_value={'ad_id': 'A1'})
        self.assertTrue(svc.is_listing_promoted('111'))
        svc.find_ad_for_listing = MagicMock(return_value=None)
        self.assertFalse(svc.is_listing_promoted('999'))


# ─────────────────────────────────────────────────────────────────────
# 5. EbayAdService.create_ad_safe — P1 ad pre-flight check
# ─────────────────────────────────────────────────────────────────────
class CreateAdSafeTests(unittest.TestCase):
    """P1: 杜绝 '新刊登立刻开 5% 广告 → 守门员立刻关掉' 反复.

    create_ad_safe 必须在以下情况下拒绝创建广告:
      - 现价撑不起目标 bid (即 max_ad < target)

    必须 fail-open (调 create_ad) 在:
      - 找不到 SKU
      - 没有 cost 数据
      - 取不到 live_price
    """

    def _make_svc(self):
        from src.services.ebay_ad_service import EbayAdService
        svc = EbayAdService.__new__(EbayAdService)
        svc.create_ad = MagicMock(return_value={'success': True, 'ad_id': 'NEW_AD'})
        return svc

    def test_safe_price_delegates_to_create_ad(self):
        """高价 → max_ad >> 5% → 应直接 create_ad."""
        svc = self._make_svc()
        with patch('src.services.repricing_guard._fetch_cost_and_listing',
                   return_value=(50.0, 'L1')), \
             patch('src.services.ebay_ad_service.requests.get') as mock_get, \
             patch('src.services.ebay_auth.EbayOAuthService') as mock_oauth:
            mock_oauth.return_value.get_valid_token.return_value = 'tok'
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                'offers': [{'pricingSummary': {'price': {'value': '200.00'}}}]
            }
            res = svc.create_ad_safe('CAMP', 'L1', sku='SKU-A', bid_percentage=5.0)
        self.assertTrue(res['success'])
        svc.create_ad.assert_called_once_with('CAMP', 'L1', 5.0)

    def test_unsafe_price_rejects(self):
        """现价刚够本 (margin≈0) → 5% 广告会亏 → 拒绝."""
        svc = self._make_svc()
        # cost=100, price=115 → 即使关广告也只剩 ~5% 净利润, 5% 广告就破死线
        with patch('src.services.repricing_guard._fetch_cost_and_listing',
                   return_value=(100.0, 'L1')), \
             patch('src.services.ebay_ad_service.requests.get') as mock_get, \
             patch('src.services.ebay_auth.EbayOAuthService') as mock_oauth:
            mock_oauth.return_value.get_valid_token.return_value = 'tok'
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                'offers': [{'pricingSummary': {'price': {'value': '115.00'}}}]
            }
            res = svc.create_ad_safe('CAMP', 'L1', sku='SKU-A', bid_percentage=5.0)
        self.assertFalse(res['success'])
        self.assertEqual(res['reason'], 'unsafe')
        self.assertIn('required_ad_rate', res)
        self.assertIn('target_ad_rate', res)
        svc.create_ad.assert_not_called()

    def test_no_cost_fails_open(self):
        """DB 无 cost → 当作信息缺失, fail-open 调 create_ad."""
        svc = self._make_svc()
        with patch('src.services.repricing_guard._fetch_cost_and_listing',
                   return_value=(0.0, 'L1')):
            res = svc.create_ad_safe('CAMP', 'L1', sku='SKU-A', bid_percentage=5.0)
        self.assertTrue(res['success'])
        svc.create_ad.assert_called_once()

    def test_no_live_price_fails_open(self):
        """API 取不到 live_price → fail-open."""
        svc = self._make_svc()
        with patch('src.services.repricing_guard._fetch_cost_and_listing',
                   return_value=(50.0, 'L1')), \
             patch('src.services.ebay_ad_service.requests.get') as mock_get, \
             patch('src.services.ebay_auth.EbayOAuthService') as mock_oauth:
            mock_oauth.return_value.get_valid_token.return_value = 'tok'
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {'offers': []}
            res = svc.create_ad_safe('CAMP', 'L1', sku='SKU-A', bid_percentage=5.0)
        self.assertTrue(res['success'])
        svc.create_ad.assert_called_once()


if __name__ == '__main__':
    unittest.main()
