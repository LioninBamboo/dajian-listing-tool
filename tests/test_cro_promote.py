"""S23 — cro_promote 出价提升执行器."""
from __future__ import annotations

import sys
from types import SimpleNamespace


def test_promote_skips_when_no_listing_id(monkeypatch):
    from scripts import cro_promote as cp
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: None)
    rep = cp._promote_one(ad_service=object(), sku='S1',
                          suggested_bid_pct=5.0)
    assert rep['status'] == 'skipped'
    assert 'no ebay_item_id' in rep['reason']


def test_promote_skips_when_not_promoted(monkeypatch):
    from scripts import cro_promote as cp
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '12345')

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return None
        def update_ad_bid(self, *a, **k):
            raise AssertionError('should not be called')

    rep = cp._promote_one(ad_service=FakeAd(), sku='S1',
                          suggested_bid_pct=5.0)
    assert rep['status'] == 'skipped'
    assert 'not promoted' in rep['reason']


def test_promote_creates_missing_ad_safely(monkeypatch):
    from scripts import cro_promote as cp
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '12345')

    captured = {}

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return None

        def fetch_campaigns(self, status='RUNNING'):
            return [{'campaignId': 'C1'}]

        def create_ad_safe(self, campaign_id, listing_id, sku=None, bid_percentage=5.0):
            captured['args'] = (campaign_id, listing_id, sku, bid_percentage)
            return {'success': True}

    rep = cp._promote_one(
        ad_service=FakeAd(),
        sku='S1',
        suggested_bid_pct=5.0,
        create_if_missing=True,
    )
    assert rep['status'] == 'done'
    assert rep['created_ad'] is True
    assert captured['args'] == ('C1', '12345', 'S1', 5.0)


def test_promote_increases_bid(monkeypatch):
    from scripts import cro_promote as cp
    from src.services import cro_margin_aware_bid as mab
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '99')
    monkeypatch.setattr(mab, 'bid_cap_for_sku', lambda *a, **k: 25.0)

    captured = {}

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return {'campaign_id': 'C1', 'ad_id': 'A1',
                    'bid_percentage': 5.0}
        def update_ad_bid(self, cid, lid, new_bid):
            captured['args'] = (cid, lid, new_bid)
            return {'success': True}

    rep = cp._promote_one(ad_service=FakeAd(), sku='S1',
                          suggested_bid_pct=4.0)
    assert rep['status'] == 'done'
    assert rep['cur_bid'] == 5.0
    assert rep['new_bid'] == 9.0
    assert captured['args'] == ('C1', '99', 9.0)


def test_promote_caps_at_max(monkeypatch):
    from scripts import cro_promote as cp
    from src.services import cro_margin_aware_bid as mab
    monkeypatch.setattr(cp, '_lookup_listing_id', lambda sku: '99')
    monkeypatch.setattr(cp, 'MAX_BID_PCT', 25.0)
    monkeypatch.setattr(mab, 'bid_cap_for_sku', lambda *a, **k: 25.0)

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return {'campaign_id': 'C1', 'ad_id': 'A1',
                    'bid_percentage': 22.0}
        def update_ad_bid(self, cid, lid, new_bid):
            return {'success': True}

    rep = cp._promote_one(ad_service=FakeAd(), sku='S1',
                          suggested_bid_pct=10.0)
    assert rep['status'] == 'done'
    assert rep['new_bid'] == 25.0  # capped


def test_run_dry_run_does_not_call_ad_service(monkeypatch):
    from scripts import cro_promote as cp
    monkeypatch.setattr(cp, 'load_pending', lambda action_type:
                        [{'sku': 'S1', 'detail': {'suggested_bid_pct': 5.0}},
                         {'sku': 'S2', 'detail': {}}])
    rep = cp.run(apply_changes=False, limit=10)
    assert rep['pending_total'] == 2
    assert all(r['status'] == 'dry_run' for r in rep['rows'])
    assert rep['done'] == [] and rep['failed'] == []


def test_run_marks_terminal_skips(monkeypatch):
    from scripts import cro_promote as cp
    monkeypatch.setitem(
        sys.modules,
        'src.services.ebay_ad_service',
        SimpleNamespace(EbayAdService=lambda: object()),
    )
    monkeypatch.setattr(cp, 'load_pending', lambda action_type:
                        [{'sku': 'S1', 'detail': {'suggested_bid_pct': 5.0}}])
    monkeypatch.setattr(cp, '_promote_one', lambda *a, **k: {
        'sku': 'S1', 'status': 'skipped', 'reason': 'already at cap',
        'terminal': True,
    })
    captured = {}
    def fake_mark_done(skus, action, result='done'):
        captured['call'] = (list(skus), action, result)
        return 1
    monkeypatch.setattr(cp, 'mark_done', fake_mark_done)
    rep = cp.run(apply_changes=True, limit=10)
    assert rep['marked_skipped'] == 1
    assert captured['call'] == (['S1'], 'promote', 'skipped')
