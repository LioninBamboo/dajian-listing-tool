"""P10 — campaign_capacity_audit tests."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts import campaign_capacity_audit as cc


def _fake_service(specs):
    """specs: [(campaign_id, ad_count)]"""
    svc = MagicMock()
    svc.fetch_campaigns.return_value = [
        {'campaignId': cid, 'campaignName': f'C-{cid}'} for cid, _ in specs
    ]
    svc.fetch_campaign_ads.side_effect = lambda cid: [
        {'adId': f'{cid}-{i}'} for i in range(dict(specs)[cid])
    ]
    return svc


def test_collect_classifies_saturated_and_available():
    svc = _fake_service([('C1', 9500), ('C2', 500), ('C3', 5000)])
    r = cc.collect_capacity(ad_service=svc)
    assert len(r['saturated']) == 1 and r['saturated'][0]['campaign_id'] == 'C1'
    assert len(r['available']) == 1 and r['available'][0]['campaign_id'] == 'C2'
    # sorted by ad_count desc
    assert [c['campaign_id'] for c in r['campaigns']] == ['C1', 'C3', 'C2']


def test_global_fill_pct():
    svc = _fake_service([('A', 5000), ('B', 5000)])
    r = cc.collect_capacity(ad_service=svc)
    assert r['total_ads'] == 10_000
    assert r['total_capacity'] == 20_000
    assert abs(r['global_fill_pct'] - 0.5) < 1e-6


def test_render_html_no_crash():
    svc = _fake_service([('Z', 100)])
    r = cc.collect_capacity(ad_service=svc)
    h = cc.render_html(r)
    assert 'Campaign 容量' in h and 'Z' in h


def test_empty_campaigns():
    svc = MagicMock()
    svc.fetch_campaigns.return_value = []
    r = cc.collect_capacity(ad_service=svc)
    assert r['campaigns'] == []
    assert r['global_fill_pct'] == 0
