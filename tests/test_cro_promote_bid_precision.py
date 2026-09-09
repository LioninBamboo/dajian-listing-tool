"""promote bid 精度与死链终态 — 2026-07-12 空转回归.

eBay Marketing API (35007) 只接受 1 位小数的 bidPercentage. 利润感知
cap 修复后产出 7.05/13.93 这类两位小数, 当日全部 update 400 空转.
listing 已结束 (35037) 之前被记为 failed, 每天无意义重试.
"""
from __future__ import annotations


def test_bid_quantized_to_one_decimal_below_cap(monkeypatch):
    from scripts import cro_promote as cp
    from src.services import cro_margin_aware_bid as mab
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '99')
    monkeypatch.setattr(mab, 'bid_cap_for_sku', lambda *a, **k: 13.93)

    captured = {}

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return {'campaign_id': 'C1', 'ad_id': 'A1', 'bid_percentage': 10.0}
        def update_ad_bid(self, cid, lid, new_bid):
            captured['new_bid'] = new_bid
            return {'success': True}

    rep = cp._promote_one(ad_service=FakeAd(), sku='S1', suggested_bid_pct=5.0)
    assert rep['status'] == 'done'
    nb = captured['new_bid']
    assert nb == round(nb, 1)          # 最多 1 位小数
    assert nb <= 13.93                 # 不越利润 cap (向下量化)
    assert nb == 13.9


def test_bid_cap_705_quantizes_to_70(monkeypatch):
    from scripts import cro_promote as cp
    from src.services import cro_margin_aware_bid as mab
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '99')
    monkeypatch.setattr(mab, 'bid_cap_for_sku', lambda *a, **k: 7.05)

    captured = {}

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return {'campaign_id': 'C1', 'ad_id': 'A1', 'bid_percentage': 5.0}
        def update_ad_bid(self, cid, lid, new_bid):
            captured['new_bid'] = new_bid
            return {'success': True}

    rep = cp._promote_one(ad_service=FakeAd(), sku='S1', suggested_bid_pct=5.0)
    assert rep['status'] == 'done'
    assert captured['new_bid'] == 7.0  # 7.05 → 7.0, 不是 7.1


def test_update_ended_listing_is_terminal_skip(monkeypatch):
    from scripts import cro_promote as cp
    from src.services import cro_margin_aware_bid as mab
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '99')
    monkeypatch.setattr(mab, 'bid_cap_for_sku', lambda *a, **k: 25.0)

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return {'campaign_id': 'C1', 'ad_id': 'A1', 'bid_percentage': 5.0}
        def update_ad_bid(self, cid, lid, new_bid):
            return {'success': False,
                    'error': 'HTTP 400: {"errors":[{"errorId":35037,'
                             '"message":"The listing ... has ended."}]}'}

    rep = cp._promote_one(ad_service=FakeAd(), sku='S1', suggested_bid_pct=5.0)
    assert rep['status'] == 'skipped'
    assert rep['terminal'] is True
    assert 'ended' in rep['reason']


def test_create_ended_listing_is_terminal_skip(monkeypatch):
    from scripts import cro_promote as cp
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '12345')

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return None
        def fetch_campaigns(self, status='RUNNING'):
            return [{'campaignId': 'C1'}]
        def create_ad_safe(self, campaign_id, listing_id, sku=None,
                           bid_percentage=5.0):
            return {'success': False,
                    'error': '35037: The listing associated with listing Id '
                             '366417646376 has ended.'}

    rep = cp._promote_one(ad_service=FakeAd(), sku='S1',
                          suggested_bid_pct=5.0, create_if_missing=True)
    assert rep['status'] == 'skipped'
    assert rep['terminal'] is True
