"""S119 — keyword gap tests."""
from __future__ import annotations

from src.services.cro_keyword_gap import (
    analyze_keyword_gap, batch_analyze, competitor_term_frequency, tokenize,
)


def test_tokenize_skips_stopwords():
    assert tokenize('the desk is in the room') == ['desk', 'room']


def test_competitor_freq_counts_unique_per_title():
    titles = ['adjustable desk', 'adjustable desk pro', 'desk pro']
    freq = competitor_term_frequency(titles)
    assert freq['desk'] == 3
    assert freq['adjustable'] == 2


def test_no_competitors_returns_empty():
    out = analyze_keyword_gap('my title', [])
    assert out['gap_terms'] == []
    assert out['competitor_count'] == 0


def test_gap_terms_when_my_title_misses_common_words():
    competitors = [
        'adjustable standing desk converter',
        'adjustable desk converter pro',
        'standing desk converter electric',
        'standing desk converter wood',
    ]
    out = analyze_keyword_gap('My Desk', competitors,
                                min_competitor_share=0.50)
    inserts = out['recommended_inserts']
    assert 'standing' in inserts or 'converter' in inserts


def test_overlap_ratio_one_when_full_match():
    competitors = ['standing desk', 'standing desk', 'standing desk']
    out = analyze_keyword_gap('Standing Desk Pro', competitors,
                                min_competitor_share=0.50)
    assert out['overlap_ratio'] == 1.0
    assert out['gap_terms'] == []


def test_threshold_filters_rare_words():
    competitors = [
        'desk',
        'desk',
        'desk neon',     # 'neon' 仅 1/3 → 33%
    ]
    out = analyze_keyword_gap('chair', competitors,
                                min_competitor_share=0.50)
    terms = {g['term'] for g in out['gap_terms']}
    assert 'neon' not in terms
    assert 'desk' in terms


def test_chinese_token_match():
    competitors = ['可调升降桌 现代款', '可调升降桌 电动款',
                    '可调升降桌 木质款']
    out = analyze_keyword_gap('普通桌子', competitors,
                                min_competitor_share=0.50)
    inserts = out['recommended_inserts']
    assert '可调升降桌' in inserts


def test_batch_analyze_flags_high_gap():
    rows = [
        {'sku': 'A', 'my_title': 'desk',
         'competitor_titles': ['standing desk converter',
                                'standing desk converter pro',
                                'standing desk converter wood']},
        {'sku': 'B', 'my_title': 'standing desk converter',
         'competitor_titles': ['standing desk converter pro',
                                'standing desk converter wood',
                                'standing desk converter electric']},
    ]
    out = batch_analyze(rows)
    assert out['count'] == 2
    assert out['high_gap_count'] == 1
