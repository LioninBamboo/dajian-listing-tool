"""ad_restore_audit 单元测试 (2026-05).

覆盖纯逻辑 (不调真 eBay):
1. evaluate_sku 三路径 — restore / skip_low_margin / skip_unsafe
2. load_published_skus 过滤掉无成本/无 listing_id
3. CLI 入参校验
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 导入脚本模块
import importlib.util
spec = importlib.util.spec_from_file_location(
    "ad_restore_audit", ROOT / "scripts" / "ad_restore_audit.py"
)
ara = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ara)


class EvaluateSkuTests(unittest.TestCase):

    def test_restore_when_headroom_is_sufficient(self):
        # cost=100, safety=5%, target=5%
        # 5% 广告 + 5% 利润缓冲 → 死线 ≈ 137.7
        sku_info = {'sku': 'S1', 'listing_id': 'L1', 'total_cost': 100.0}
        # 现价 150 远高于 137.7 → 应恢复
        result = ara.evaluate_sku(sku_info, live_price=150.0,
                                   target_bid=5.0, safety_margin=0.05)
        self.assertEqual(result['decision'], 'restore')
        self.assertGreater(result['required_ad_rate'], 0.05)

    def test_skip_low_margin_when_below_target(self):
        # safe_floor_no_ad(safety=5%) ≈ 127.77, safe_floor_with_5%_ad ≈ 135.59
        # 现价 130 落在中间 → 撑得起 0~5% 之间的广告 → skip_low_margin
        sku_info = {'sku': 'S2', 'listing_id': 'L2', 'total_cost': 100.0}
        result = ara.evaluate_sku(sku_info, live_price=130.0,
                                   target_bid=5.0, safety_margin=0.05)
        self.assertEqual(result['decision'], 'skip_low_margin')
        self.assertGreaterEqual(result['required_ad_rate'], 0)
        self.assertLess(result['required_ad_rate'], 0.05)

    def test_skip_unsafe_when_price_at_break_even(self):
        # 现价低到连 5% 利润缓冲 (无广告) 都撑不到 → required_ad_rate < 0
        sku_info = {'sku': 'S3', 'listing_id': 'L3', 'total_cost': 100.0}
        result = ara.evaluate_sku(sku_info, live_price=125.0,
                                   target_bid=5.0, safety_margin=0.05)
        self.assertEqual(result['decision'], 'skip_unsafe')
        self.assertLess(result['required_ad_rate'], 0)

    def test_zero_safety_margin_lowers_threshold(self):
        # 同样 130 价位, safety=0 时门槛更低 → required 更高
        sku_info = {'sku': 'S4', 'listing_id': 'L4', 'total_cost': 100.0}
        r5 = ara.evaluate_sku(sku_info, 130.0, 5.0, 0.05)
        r0 = ara.evaluate_sku(sku_info, 130.0, 5.0, 0.0)
        self.assertGreater(r0['required_ad_rate'], r5['required_ad_rate'])


class LoadPublishedSkusTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / 'test.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE collected_products (
                sku TEXT PRIMARY KEY, title TEXT, status TEXT,
                listing_id TEXT, cost_breakdown TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO collected_products VALUES (?,?,?,?,?)",
            [
                ('OK-1', 'T1', 'PUBLISHED', 'L1', json.dumps({'total_dajian_cost': 50})),
                ('OK-2', 'T2', 'PUBLISHED', 'L2', json.dumps({'total_dajian_cost': 100})),
                # 排除: status != PUBLISHED
                ('SKIP-DRAFT', 'T3', 'READY', 'L3', json.dumps({'total_dajian_cost': 50})),
                # 排除: 无 listing_id
                ('SKIP-NOLID', 'T4', 'PUBLISHED', '', json.dumps({'total_dajian_cost': 50})),
                # 排除: 无 cost_breakdown
                ('SKIP-NOCB', 'T5', 'PUBLISHED', 'L5', None),
                # 排除: total_dajian_cost = 0
                ('SKIP-ZERO', 'T6', 'PUBLISHED', 'L6', json.dumps({'total_dajian_cost': 0})),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_filters_correctly(self):
        with patch.object(ara, 'DB_PATH', self.db_path):
            rows = ara.load_published_skus()
        skus = sorted(r['sku'] for r in rows)
        self.assertEqual(skus, ['OK-1', 'OK-2'])
        # 验证字段
        for r in rows:
            self.assertIn('total_cost', r)
            self.assertGreater(r['total_cost'], 0)
            self.assertTrue(r['listing_id'])


class RunAuditRegressionTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_run_audit_skips_stale_listing_without_create_attempt(self):
        fake_ad_svc = MagicMock()
        fake_ad_svc.fetch_campaigns.return_value = [
            {'campaignId': 'C1', 'campaignName': 'Test Campaign'}
        ]
        fake_ad_svc.fetch_campaign_ads.return_value = []
        fake_ad_svc.create_ad_safe.return_value = {'success': False, 'error': 'should not be called'}

        candidate = {
            'sku': 'SKU-1',
            'title': 'Test',
            'listing_id': 'LOCAL-L1',
            'total_cost': 50.0,
        }

        with patch.object(ara, 'LOG_DIR', self.log_dir), \
             patch.object(ara, 'load_published_skus', return_value=[candidate]), \
             patch('src.services.ebay_auth.EbayOAuthService') as mock_oauth, \
             patch('src.services.ebay_ad_service.EbayAdService', return_value=fake_ad_svc), \
             patch.object(ara.requests, 'get') as mock_get:
            mock_oauth.return_value.get_valid_token.return_value = 'tok'
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                'offers': [{
                    'marketplaceId': 'EBAY_US',
                    'status': 'PUBLISHED',
                    'listingId': 'OTHER-L2',
                    'pricingSummary': {'price': {'value': '150.00'}},
                }]
            }

            report = ara.run_audit(
                apply_changes=True,
                send_email=False,
                target_bid=5.0,
                safety_margin=0.05,
            )

        self.assertIn('skip_not_live', report['totals'])
        self.assertEqual(report['totals']['should_restore'], 0)
        self.assertEqual(report['totals']['skip_not_live'], 1)
        self.assertEqual(report['totals']['restore_failed'], 0)
        fake_ad_svc.create_ad_safe.assert_not_called()


class EmailAndExitCodeRegressionTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_send_email_uses_generic_sender_with_attachment(self):
        report = {
            'mode': 'apply',
            'totals': {
                'evaluated_not_promoted': 3,
                'should_restore': 1,
                'restored_ok': 1,
                'restore_failed': 0,
                'skip_low_margin': 1,
                'skip_unsafe': 0,
            }
        }
        report_path = Path(self.tmpdir.name) / 'report.json'
        report_path.write_text('{}', encoding='utf-8')

        with patch('src.utils.email_sender.send_email', return_value=True) as mock_send:
            ara._send_email(report, report_path)

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        self.assertEqual(kwargs['attachments'], [str(report_path)])

    def test_main_exits_zero_for_nonfatal_restore_failures(self):
        with patch.object(ara, 'run_audit', return_value={'totals': {'restore_failed': 2}}), \
             patch.object(sys, 'argv', ['ad_restore_audit.py']):
            with self.assertRaises(SystemExit) as cm:
                ara.main()
        self.assertEqual(cm.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
