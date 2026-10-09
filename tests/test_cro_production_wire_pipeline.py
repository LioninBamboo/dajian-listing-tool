"""S121 — production wire pipeline tests."""
from __future__ import annotations

from src.services.cro_production_wire_pipeline import (
    build_pipeline, from_cross_category_arb, from_keyword_gap,
    from_returns_prevention, from_search_query_match, from_supplier_risk,
)


def test_returns_pause_and_review():
    rep = {
        'pause_candidates': [{'sku': 'A', 'reason': 'r', 'fix_hint': 'h',
                               'return_rate': 0.3}],
        'review_candidates': [{'sku': 'B', 'reason': 'r2',
                                'return_rate': 0.12}],
    }
    rows = from_returns_prevention(rep)
    assert {r['sku'] for r in rows} == {'A', 'B'}
    actions = {r['action_type'] for r in rows}
    assert 'pause_candidate' in actions
    assert 'review' in actions


def test_search_match_skips_when_no_inserts():
    rep = {'items': [{'sku': 'A', 'recommended_inserts': []},
                      {'sku': 'B', 'recommended_inserts': ['x']}]}
    rows = from_search_query_match(rep)
    assert len(rows) == 1
    assert rows[0]['sku'] == 'B'
    assert rows[0]['action_type'] == 'title_insert'


def test_keyword_gap_skips_high_overlap():
    rep = {'items': [
        {'sku': 'A', 'overlap_ratio': 0.80, 'recommended_inserts': ['x']},
        {'sku': 'B', 'overlap_ratio': 0.20, 'recommended_inserts': ['y']},
    ]}
    rows = from_keyword_gap(rep)
    assert [r['sku'] for r in rows] == ['B']


def test_cross_category_arb_passthrough():
    opps = [{'sku': 'A', 'target_category': 5, 'recommended_price': 19.99,
              'lift': 0.5}]
    rows = from_cross_category_arb(opps)
    assert rows[0]['action_type'] == 'category_migrate'
    assert rows[0]['payload']['target_category'] == 5


def test_supplier_risk_only_high_critical():
    rep = {'items': [
        {'supplier_id': 'S1', 'risk_label': 'critical'},
        {'supplier_id': 'S2', 'risk_label': 'low'},
    ]}
    sku_map = {'A': 'S1', 'B': 'S2'}
    rows = from_supplier_risk(rep, sku_map)
    assert {r['sku'] for r in rows} == {'A'}


def test_build_pipeline_dedup_same_sku_action():
    def returns():
        return {'pause_candidates': [{'sku': 'A', 'reason': 'r',
                                       'return_rate': 0.3}]}
    out = build_pipeline(returns_report_fn=returns)
    out2 = build_pipeline(returns_report_fn=returns,
                           search_match_report_fn=returns)
    # 双源同 (A, pause_candidate) 不重复
    assert out['count'] == 1
    assert out2['count'] == 1


def test_build_pipeline_blacklist_filter():
    def returns():
        return {'pause_candidates': [{'sku': 'A'}, {'sku': 'B'}]}
    out = build_pipeline(returns_report_fn=returns, blacklist=['A'])
    skus = {r['sku'] for r in out['rows']}
    assert skus == {'B'}
    assert out['skipped_blacklisted'] == 1


def test_build_pipeline_swallows_fetcher_exception():
    def boom():
        raise RuntimeError('fail')
    out = build_pipeline(returns_report_fn=boom,
                          keyword_gap_report_fn=boom)
    assert out['count'] == 0   # 异常 → 空 → 不抛


def test_build_pipeline_aggregates_by_action():
    def returns():
        return {'pause_candidates': [{'sku': 'A'}]}
    def search():
        return {'items': [{'sku': 'B', 'recommended_inserts': ['x']}]}
    out = build_pipeline(returns_report_fn=returns,
                          search_match_report_fn=search)
    assert out['by_action'].get('pause_candidate') == 1
    assert out['by_action'].get('title_insert') == 1


def test_skips_rows_without_sku():
    def returns():
        return {'pause_candidates': [{'sku': None}, {'sku': 'A'}]}
    out = build_pipeline(returns_report_fn=returns)
    assert {r['sku'] for r in out['rows']} == {'A'}
