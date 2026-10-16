"""S114 — search query match tests."""
from __future__ import annotations

from src.services.cro_search_query_match import (
    batch_score, recommend_title_inserts, score_title_match, tokenize_lower,
)


def test_tokenize_handles_punctuation():
    assert tokenize_lower('Adjustable Stand-Up Desk!') == [
        'adjustable', 'stand', 'up', 'desk']


def test_score_no_queries_zero_coverage():
    out = score_title_match('Adjustable Desk', [])
    assert out['coverage'] == 0.0
    assert out['total_impressions'] == 0


def test_score_full_match_coverage_one():
    queries = [{'query': 'adjustable desk', 'impressions': 100}]
    out = score_title_match('Adjustable Desk Pro', queries)
    assert out['coverage'] == 1.0
    assert out['covered_impressions'] == 100


def test_score_partial_match_records_missing():
    queries = [
        {'query': 'standing desk converter', 'impressions': 100},
        {'query': 'adjustable desk', 'impressions': 50},
    ]
    out = score_title_match('Adjustable Desk Pro', queries)
    assert out['coverage'] < 1.0
    missing = {m['term'] for m in out['missing_terms']}
    assert 'standing' in missing or 'converter' in missing


def test_score_chinese_token_match():
    queries = [{'query': '可调升降桌', 'impressions': 100}]
    out = score_title_match('可调升降桌 Pro', queries)
    # 中文连续字符 → 整体 token, 命中
    assert out['coverage'] == 1.0


def test_recommend_inserts_respects_char_budget():
    score = {
        'title_length': 75,
        'missing_terms': [
            {'term': 'standing', 'missing_impressions': 100},
            {'term': 'converter', 'missing_impressions': 80},
            {'term': 'pro', 'missing_impressions': 50},
        ],
    }
    out = recommend_title_inserts(score)
    # 预算 = 80 - 75 = 5; 'pro'(3+1=4) 可加, 别的不行
    assert 'pro' in out
    assert 'standing' not in out


def test_recommend_inserts_top_n_cap():
    score = {
        'title_length': 0,
        'missing_terms': [
            {'term': str(i), 'missing_impressions': 100} for i in range(10)
        ],
    }
    out = recommend_title_inserts(score, top_n=3)
    assert len(out) == 3


def test_batch_score_flags_low_coverage():
    rows = [
        {'sku': 'A', 'title': 'Adjustable Desk',
         'top_queries': [{'query': 'standing desk converter',
                           'impressions': 1000}]},
        {'sku': 'B', 'title': 'Adjustable Desk',
         'top_queries': [{'query': 'adjustable desk', 'impressions': 100}]},
    ]
    out = batch_score(rows)
    assert out['count'] == 2
    assert out['low_coverage_count'] == 1
