"""ad_restore_audit Trading 回退 — Inventory API 对 Trading 系 listing 失明的修复.

2026-07-12 审计: 283 个未推广 SKU 全部 skip_not_live ('无 PUBLISHED offer'),
实测其中 listing 366226629185 在 eBay 上 Active — 因为 Trading API 创建的
listing 在 Inventory offer API 里返回空. offers 为空时必须回退 Trading GetItem.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "ad_restore_audit_tf", ROOT / "scripts" / "ad_restore_audit.py"
)
ara = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ara)

_NS = 'urn:ebay:apis:eBLBaseComponents'


def _get_item_xml(status: str, price: str = '230.35') -> str:
    return (f'<GetItemResponse xmlns="{_NS}"><Item>'
            f'<SellingStatus><ListingStatus>{status}</ListingStatus>'
            f'<CurrentPrice currencyID="USD">{price}</CurrentPrice>'
            f'</SellingStatus></Item></GetItemResponse>')


class TradingFallbackTests(unittest.TestCase):

    def _empty_offers_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'offers': []}
        return mock_resp

    def test_empty_offers_with_active_trading_listing_is_live(self):
        trading = MagicMock()
        trading.call.return_value = _get_item_xml('Active')
        with patch.object(ara.requests, 'get',
                          return_value=self._empty_offers_response()):
            oauth = MagicMock()
            oauth.get_valid_token.return_value = 'tok'
            state = ara.fetch_live_listing_state(
                oauth, 'SKU1', expected_listing_id='366226629185',
                trading=trading)
        self.assertEqual(state['state'], 'live')
        self.assertEqual(state['live_price'], 230.35)
        self.assertEqual(state['channel'], 'trading_api')
        trading.call.assert_called_once()

    def test_empty_offers_with_ended_trading_listing_stays_not_live(self):
        trading = MagicMock()
        trading.call.return_value = _get_item_xml('Completed')
        with patch.object(ara.requests, 'get',
                          return_value=self._empty_offers_response()):
            oauth = MagicMock()
            oauth.get_valid_token.return_value = 'tok'
            state = ara.fetch_live_listing_state(
                oauth, 'SKU1', expected_listing_id='123', trading=trading)
        self.assertEqual(state['state'], 'not_live')

    def test_empty_offers_without_trading_client_keeps_old_behavior(self):
        with patch.object(ara.requests, 'get',
                          return_value=self._empty_offers_response()):
            oauth = MagicMock()
            oauth.get_valid_token.return_value = 'tok'
            state = ara.fetch_live_listing_state(
                oauth, 'SKU1', expected_listing_id='123')
        self.assertEqual(state['state'], 'not_live')
        self.assertIn('无 PUBLISHED offer', state['reason'])

    def test_mismatched_offers_do_not_trigger_trading_fallback(self):
        # Inventory 有别的 PUBLISHED listing → 本地映射过期, 不走 Trading
        trading = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'offers': [{
            'marketplaceId': 'EBAY_US', 'status': 'PUBLISHED',
            'listingId': 'OTHER', 'pricingSummary': {'price': {'value': '10'}},
        }]}
        with patch.object(ara.requests, 'get', return_value=mock_resp):
            oauth = MagicMock()
            oauth.get_valid_token.return_value = 'tok'
            state = ara.fetch_live_listing_state(
                oauth, 'SKU1', expected_listing_id='123', trading=trading)
        self.assertEqual(state['state'], 'not_live')
        trading.call.assert_not_called()

    def test_trading_error_degrades_to_not_live(self):
        trading = MagicMock()
        trading.call.side_effect = RuntimeError('timeout')
        with patch.object(ara.requests, 'get',
                          return_value=self._empty_offers_response()):
            oauth = MagicMock()
            oauth.get_valid_token.return_value = 'tok'
            state = ara.fetch_live_listing_state(
                oauth, 'SKU1', expected_listing_id='123', trading=trading)
        self.assertEqual(state['state'], 'not_live')


if __name__ == '__main__':
    unittest.main()
