"""Smart Bid 7 天回溯测试 (P5, 2026-05)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import bid_rollback_audit as bra


class FindBaselineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _make_report(self, days_ago: int):
        d = (datetime.now() - timedelta(days=days_ago)).strftime('%Y%m%d')
        path = self.tmp / f'batch_smart_bid_{d}_120000.json'
        path.write_text('{"plan": []}', encoding='utf-8')
        return path

    def test_finds_exact_lookback(self):
        path = self._make_report(7)
        result = bra.find_baseline_report(7, log_dir=self.tmp)
        self.assertEqual(result, path)

    def test_falls_back_to_nearest_within_2_days(self):
        path = self._make_report(8)  # 8 天前 → 距离 7 = 1 天
        result = bra.find_baseline_report(7, log_dir=self.tmp)
        self.assertEqual(result, path)

    def test_returns_none_when_too_far(self):
        self._make_report(20)
        result = bra.find_baseline_report(7, log_dir=self.tmp)
        self.assertIsNone(result)

    def test_returns_none_for_empty_dir(self):
        result = bra.find_baseline_report(7, log_dir=self.tmp)
        self.assertIsNone(result)


class ExtractBidRaisesTests(unittest.TestCase):

    def test_only_includes_applied_raises_above_mid(self):
        baseline = {'plan': [
            {'sku': 'A', 'decision': 'adjust', 'applied': True,
             'current_bid': 5.0, 'new_bid': 7.0},
            {'sku': 'B', 'decision': 'adjust', 'applied': False,
             'current_bid': 5.0, 'new_bid': 7.0},  # 未 applied
            {'sku': 'C', 'decision': 'adjust', 'applied': True,
             'current_bid': 5.0, 'new_bid': 3.0},  # 不是提升
            {'sku': 'D', 'decision': 'no_change', 'applied': True,
             'current_bid': 5.0, 'new_bid': 5.0},
            {'sku': 'E', 'decision': 'adjust', 'applied': True,
             'current_bid': 7.0, 'new_bid': 7.5},  # 已经 >MID, 不算提升
        ]}
        raises = bra.extract_bid_raises(baseline)
        self.assertEqual([r['sku'] for r in raises], ['A'])


class EvaluateRollbackTests(unittest.TestCase):

    def _baseline(self, **kw):
        base = {'sku': 'X', 'ctr': 0.01, 'sold_qty': 0,
                'current_bid': 5.0, 'new_bid': 7.0, 'listing_id': 'L',
                'campaign_id': 'C'}
        base.update(kw)
        return base

    def test_no_data_when_low_impressions(self):
        d = bra.evaluate_rollback(self._baseline(), {'impressions': 10, 'ctr': 0.05, 'sold_qty': 0})
        self.assertEqual(d['decision'], 'no_data')

    def test_rollback_when_no_improvement(self):
        d = bra.evaluate_rollback(
            self._baseline(ctr=0.01, sold_qty=0),
            {'impressions': 500, 'ctr': 0.011, 'sold_qty': 0},
        )
        self.assertEqual(d['decision'], 'rollback')
        self.assertEqual(d['old_bid_target'], bra.MID_BID)

    def test_keep_when_ctr_improved(self):
        d = bra.evaluate_rollback(
            self._baseline(ctr=0.01, sold_qty=0),
            {'impressions': 500, 'ctr': 0.025, 'sold_qty': 0},  # CTR +1.5%
        )
        self.assertEqual(d['decision'], 'keep')

    def test_keep_when_sales_improved(self):
        d = bra.evaluate_rollback(
            self._baseline(ctr=0.02, sold_qty=0),
            {'impressions': 500, 'ctr': 0.02, 'sold_qty': 2},
        )
        self.assertEqual(d['decision'], 'keep')


if __name__ == '__main__':
    unittest.main()
