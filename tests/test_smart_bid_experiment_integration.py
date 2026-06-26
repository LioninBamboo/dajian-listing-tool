"""P14 — A/B 实验接入 batch_smart_bid.build_plan tests."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from scripts import batch_smart_bid as bsb


def _ad(lid, bid=5.0, cid='C1'):
    return {'listing_id': lid, 'campaign_id': cid, 'bid_percentage': bid}


def test_no_experiment_falls_back_to_tier_logic():
    products = [{'sku': 'A', 'listing_id': 'L1', 'impressions': 1000, 'ctr': 0.05, 'sold_qty': 5}]
    ads = {'L1': _ad('L1', bid=5.0)}
    cost = {'A': {'total_cost': 20.0, 'listing_id': 'L1'}}
    plan = bsb.build_plan(products, ads, cost, real_client=None, active_experiments=None)
    assert len(plan) == 1
    assert plan[0]['decision'] in ('adjust', 'no_change')
    assert 'experiment_id' not in plan[0]


def test_experiment_overrides_desired_bid_variant():
    products = [{'sku': 'A', 'listing_id': 'L1', 'impressions': 1000, 'ctr': 0.05, 'sold_qty': 5}]
    ads = {'L1': _ad('L1', bid=5.0)}
    cost = {'A': {'total_cost': 20.0, 'listing_id': 'L1'}}
    exps = [{'experiment_id': 'EXP1', 'control_bid': 5.0, 'variant_bid': 8.0,
             'assignments': {'A': 'variant'}}]
    plan = bsb.build_plan(products, ads, cost, real_client=None, active_experiments=exps)
    assert plan[0]['decision'] == 'experiment'
    assert plan[0]['experiment_id'] == 'EXP1'
    assert plan[0]['experiment_arm'] == 'variant'
    # cap_bid_by_floor 没现价数据 → 直接通过 desired
    assert plan[0]['new_bid'] == 8.0


def test_experiment_control_arm_uses_control_bid():
    products = [{'sku': 'B', 'listing_id': 'L2', 'impressions': 1000, 'ctr': 0.001, 'sold_qty': 0}]
    ads = {'L2': _ad('L2', bid=5.0)}
    cost = {'B': {'total_cost': 20.0, 'listing_id': 'L2'}}
    # 没实验时这个 SKU 会被分到 low → 3.0; 但 control arm = 5.0 → no_change
    exps = [{'experiment_id': 'EXP2', 'control_bid': 5.0, 'variant_bid': 8.0,
             'assignments': {'B': 'control'}}]
    plan = bsb.build_plan(products, ads, cost, real_client=None, active_experiments=exps)
    assert plan[0]['decision'] == 'no_change'  # arm_bid 5.0 == current 5.0


def test_experiment_does_not_override_unsafe_check():
    """实验仍受守门员复核 — 现价不安全则降级 skip_unsafe."""
    class FakeRealClient:
        def get_offer_by_sku(self, sku):
            # cost 20, price 10 → 即 0% 广告也亏
            return {'pricingSummary': {'price': {'value': '10'}}}

    products = [{'sku': 'C', 'listing_id': 'L3', 'impressions': 1000, 'ctr': 0.05, 'sold_qty': 5}]
    ads = {'L3': _ad('L3', bid=5.0)}
    cost = {'C': {'total_cost': 20.0, 'listing_id': 'L3'}}
    exps = [{'experiment_id': 'EXP3', 'control_bid': 5.0, 'variant_bid': 8.0,
             'assignments': {'C': 'variant'}}]
    plan = bsb.build_plan(products, ads, cost, real_client=FakeRealClient(),
                           active_experiments=exps)
    assert plan[0]['decision'] == 'skip_unsafe'


def test_load_active_experiments_handles_missing_module(monkeypatch):
    # 模拟 bid_experiment.list_experiments 抛错 → 回退空 list
    import scripts.batch_smart_bid as m
    def boom(*a, **kw):
        raise RuntimeError("db locked")
    monkeypatch.setattr('src.services.bid_experiment.list_experiments', boom)
    assert m._load_active_experiments() == []
