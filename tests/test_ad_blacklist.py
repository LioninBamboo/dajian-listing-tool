"""SKU 广告黑名单测试 (P4, 2026-05)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import ad_blacklist as bl


class AdBlacklistFileTests(unittest.TestCase):

    def setUp(self):
        self.tmpfile = Path(tempfile.mkdtemp()) / 'bl.json'

    def test_empty_returns_no_blacklist(self):
        self.assertFalse(bl.is_blacklisted('SKU-X', path=self.tmpfile))
        self.assertEqual(bl.list_all(path=self.tmpfile), {})

    def test_add_manual_then_blacklisted(self):
        e = bl.add_manual('SKU-A', note='trouble', path=self.tmpfile)
        self.assertEqual(e['reason'], 'manual')
        self.assertTrue(bl.is_blacklisted('SKU-A', path=self.tmpfile))

    def test_remove(self):
        bl.add_manual('SKU-B', path=self.tmpfile)
        self.assertTrue(bl.remove('SKU-B', path=self.tmpfile))
        self.assertFalse(bl.is_blacklisted('SKU-B', path=self.tmpfile))
        self.assertFalse(bl.remove('SKU-NONE', path=self.tmpfile))

    def test_bump_off_count_promotes_to_blacklist_at_threshold(self):
        for i in range(bl.AUTO_BLACKLIST_THRESHOLD - 1):
            entry = bl.bump_off_count('SKU-C', path=self.tmpfile)
            self.assertEqual(entry['reason'], 'tracking')
            self.assertFalse(bl.is_blacklisted('SKU-C', path=self.tmpfile))
        # 第 N 次达到阈值
        entry = bl.bump_off_count('SKU-C', path=self.tmpfile)
        self.assertEqual(entry['reason'], 'auto_off_streak')
        self.assertEqual(entry['off_count'], bl.AUTO_BLACKLIST_THRESHOLD)
        self.assertTrue(bl.is_blacklisted('SKU-C', path=self.tmpfile))

    def test_manual_not_overridden_by_bump(self):
        bl.add_manual('SKU-D', note='m', path=self.tmpfile)
        e = bl.bump_off_count('SKU-D', path=self.tmpfile)
        self.assertEqual(e['reason'], 'manual')  # 不被改成 auto_off_streak

    def test_empty_sku_safe(self):
        self.assertFalse(bl.is_blacklisted('', path=self.tmpfile))
        self.assertEqual(bl.bump_off_count('', path=self.tmpfile), {})


class AdBlacklistIntegrationWithCreateAdSafeTests(unittest.TestCase):
    """create_ad_safe 必须先检查黑名单, 命中即拒绝."""

    def setUp(self):
        from unittest.mock import patch
        self.tmpfile = Path(tempfile.mkdtemp()) / 'bl.json'
        self._patch = patch.object(bl, 'DEFAULT_PATH', self.tmpfile)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_blacklisted_sku_rejected_before_pricing_check(self):
        from unittest.mock import MagicMock
        from src.services.ebay_ad_service import EbayAdService

        bl.add_manual('SKU-BANNED', note='test', path=self.tmpfile)

        svc = EbayAdService.__new__(EbayAdService)
        svc.create_ad = MagicMock(return_value={'success': True})
        res = svc.create_ad_safe('CAMP', 'L1', sku='SKU-BANNED', bid_percentage=5.0)

        self.assertFalse(res['success'])
        self.assertEqual(res['reason'], 'blacklisted')
        svc.create_ad.assert_not_called()


if __name__ == '__main__':
    unittest.main()
