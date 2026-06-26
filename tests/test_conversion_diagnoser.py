"""转化率诊断器 (CRO) tests — 主线: 竞争监控 → 提高转化率."""
from __future__ import annotations

import pytest

from src.services.conversion_diagnoser import (
    diagnose_sku, diagnose_batch, summarize, top_actions,
    HEALTHY_CTR, HEALTHY_CVR, MIN_IMPRESSIONS_FOR_DIAGNOSIS,
)


def _p(sku='A', listing_id='L1', imp=1000, views=20, tx=1, sold=1,
       price=100.0, age=30, title='x' * 70, images=None):
    return {
        'sku': sku, 'listing_id': listing_id,
        'impressions': imp, 'views': views,
        'transactions': tx, 'sold_qty': sold,
        'selling_price': price, 'age_days': age,
        'title': title, 'images': images or ['u'],
    }


def test_no_impression_with_old_age_recommends_promote():
    d = diagnose_sku(_p(imp=0, views=0, tx=0, sold=0, age=20), market_median=100)
    assert d.funnel_stage == 'no_impression'
    assert any(a.type == 'promote' for a in d.actions)


def test_insufficient_data_returns_neutral_score():
    d = diagnose_sku(_p(imp=10, views=0, tx=0, sold=0), market_median=100)
    assert d.funnel_stage == 'insufficient_data'
    assert d.cro_score == 50


def test_low_ctr_overpriced_recommends_price_drop_p1():
    # CTR = 5/1000 = 0.5% < 1.5%, 价 130 vs 中位 100 → overpriced
    d = diagnose_sku(_p(imp=1000, views=5, tx=0, sold=0, price=130), market_median=100)
    assert d.funnel_stage == 'low_ctr'
    p1 = [a for a in d.actions if a.priority == 1]
    assert any(a.type == 'price_drop' for a in p1)


def test_low_ctr_aligned_price_short_title_recommends_title_refresh():
    # CTR 低, 价对齐, 标题短 → 标题刷新
    d = diagnose_sku(_p(imp=1000, views=5, tx=0, sold=0, price=100,
                          title='short title'), market_median=100)
    assert d.funnel_stage == 'low_ctr'
    assert any(a.type == 'title_refresh' for a in d.actions)


def test_low_ctr_aligned_price_long_title_recommends_image_refresh():
    d = diagnose_sku(_p(imp=1000, views=5, tx=0, sold=0, price=100,
                          title='x' * 80), market_median=100)
    assert any(a.type == 'image_refresh' for a in d.actions)


def test_low_cvr_overpriced_recommends_price_drop():
    # CTR ok (3%), CVR 低 (1/30=3.3%>2%? need lower). Make CVR < 2%
    # views=100, tx=1 → CVR=1%
    d = diagnose_sku(_p(imp=2000, views=100, tx=1, sold=1, price=130), market_median=100)
    assert d.funnel_stage == 'low_cvr'
    assert any(a.type == 'price_drop' and a.priority == 1 for a in d.actions)


def test_low_cvr_aligned_price_recommends_fill_specifics():
    d = diagnose_sku(_p(imp=2000, views=100, tx=1, sold=1, price=100), market_median=100)
    assert d.funnel_stage == 'low_cvr'
    assert any(a.type == 'fill_specifics' for a in d.actions)


def test_healthy_funnel_no_actions_or_only_minor():
    # CTR=2%, CVR=3%, STR healthy
    d = diagnose_sku(_p(imp=1000, views=20, tx=1, sold=1, price=100), market_median=100)
    assert d.funnel_stage == 'healthy'
    # 不应该出 P1 P2 改价/标题/图片
    urgent = [a for a in d.actions if a.priority <= 2]
    assert all(a.type not in ('price_drop', 'title_refresh', 'image_refresh',
                                'fill_specifics') for a in urgent)


def test_underpriced_with_sales_suggests_price_increase():
    # 价 70 vs 中位 100 → underpriced
    d = diagnose_sku(_p(imp=2000, views=80, tx=5, sold=5, price=70), market_median=100)
    assert any(a.type == 'price_drop' and a.detail.get('is_increase') for a in d.actions)


def test_long_zero_sales_with_traffic_recommends_promote():
    d = diagnose_sku(_p(imp=500, views=10, tx=0, sold=0, price=100, age=45), market_median=100)
    promotes = [a for a in d.actions if a.type == 'promote']
    assert promotes  # 已有展示但 30+ 天零销售


def test_diagnose_batch_uses_market_data_by_category():
    products = [
        {**_p(sku='A', price=120), 'categoryId': '38208'},
        {**_p(sku='B', price=50), 'categoryId': '38204'},
    ]
    md = {'38208': {'median': 100}, '38204': {'median': 100}}
    out = diagnose_batch(products, market_data=md)
    assert len(out) == 2
    assert {d.sku for d in out} == {'A', 'B'}


def test_summarize_aggregates_correctly():
    diags = diagnose_batch([
        {**_p(sku='A', imp=0), 'categoryId': 'X'},
        {**_p(sku='B', imp=1000, views=5, price=130), 'categoryId': 'X'},
        {**_p(sku='C', imp=1000, views=20, tx=1), 'categoryId': 'X'},
    ], market_data={'X': {'median': 100}})
    s = summarize(diags)
    assert s['total'] == 3
    assert s['by_funnel_stage'].get('no_impression') == 1
    assert s['by_funnel_stage'].get('low_ctr') == 1
    assert s['by_action_type'].get('price_drop', 0) >= 1


def test_top_actions_sorts_by_priority_then_impressions():
    diags = diagnose_batch([
        {**_p(sku='HIGH_IMP', imp=10000, views=50, price=130), 'categoryId': 'X'},
        {**_p(sku='LOW_IMP', imp=200, views=1, price=130), 'categoryId': 'X'},
    ], market_data={'X': {'median': 100}})
    rows = top_actions(diags)
    assert rows[0]['sku'] == 'HIGH_IMP'  # 高曝光 P1 先做


def test_top_actions_filter_by_type():
    diags = diagnose_batch([
        {**_p(sku='A', imp=1000, views=5, price=130), 'categoryId': 'X'},
    ], market_data={'X': {'median': 100}})
    only_price = top_actions(diags, action_type='price_drop')
    assert all(r['action'] == 'price_drop' for r in only_price)


def test_summarize_empty_returns_total_zero():
    assert summarize([]) == {'total': 0}
