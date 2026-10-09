"""eBay 广告服务测试."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.ebay_ad_service import EbayAdService


class UpdateAdBidTests(unittest.TestCase):

    def setUp(self):
        self.svc = EbayAdService.__new__(EbayAdService)
        self.svc.base = 'https://api.ebay.com'
        self.svc._headers = lambda: {'Authorization': 'Bearer test'}
        self.svc._build_ad_lookup = lambda campaign_id: {'L1': 'A1'}

    @patch('src.services.ebay_ad_service.requests.post')
    @patch('src.services.ebay_ad_service.requests.put')
    def test_update_ad_bid_posts_to_update_bid_endpoint(self, put_mock, post_mock):
        post_mock.return_value.status_code = 204
        post_mock.return_value.text = ''

        res = self.svc.update_ad_bid('C1', 'L1', 7.0)

        self.assertTrue(res['success'])
        put_mock.assert_not_called()
        post_mock.assert_called_once_with(
            'https://api.ebay.com/sell/marketing/v1/ad_campaign/C1/ad/A1/update_bid',
            headers={'Authorization': 'Bearer test'},
            json={'bidPercentage': '7.0'},
            timeout=30,
            verify=False,
        )


if __name__ == '__main__':
    unittest.main()
