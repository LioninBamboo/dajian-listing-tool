"""S96 — review sentiment tests."""
from __future__ import annotations

from src.services.cro_review_sentiment import (
    aggregate, analyze_review, classify_with_keywords, classify_with_llm,
)


def test_keyword_positive():
    out = classify_with_keywords('great product, love it')
    assert out['sentiment'] == 'positive'
    assert out['pos_hits'] >= 2


def test_keyword_negative():
    out = classify_with_keywords('terrible quality, broken on arrival')
    assert out['sentiment'] == 'negative'


def test_keyword_neutral_when_balanced():
    out = classify_with_keywords('good but bad')
    assert out['sentiment'] == 'neutral'


def test_keyword_neutral_when_no_match():
    out = classify_with_keywords('it arrived')
    assert out['sentiment'] == 'neutral'
    assert out['pos_hits'] == 0
    assert out['neg_hits'] == 0


def test_keyword_handles_none_or_empty():
    assert classify_with_keywords('')['sentiment'] == 'neutral'
    assert classify_with_keywords(None)['sentiment'] == 'neutral'


def test_keyword_chinese():
    out = classify_with_keywords('质量很好, 很满意')
    assert out['sentiment'] == 'positive'


def test_llm_used_when_keyword_neutral_no_hits():
    out = analyze_review('一般般', llm_call=lambda p: 'negative')
    assert out['sentiment'] == 'negative'
    assert out['source'] == 'llm'


def test_llm_skipped_when_keyword_has_hits():
    out = analyze_review('great', llm_call=lambda p: 'negative')
    assert out['sentiment'] == 'positive'
    assert out['source'] == 'keyword'


def test_classify_with_llm_exception_neutral():
    def bad(p):
        raise RuntimeError('api')
    out = classify_with_llm('hi', bad)
    assert out['sentiment'] == 'neutral'
    assert out['source'] == 'llm_error'


def test_aggregate_counts_and_pcts():
    reviews = ['great', 'bad', 'awesome', '一般般']
    out = aggregate(reviews)
    assert out['count'] == 4
    assert out['by_sentiment']['positive'] == 2
    assert out['by_sentiment']['negative'] == 1
    assert out['positive_pct'] == 0.5


def test_aggregate_empty():
    out = aggregate([])
    assert out['count'] == 0
    assert out['avg_score'] == 0.0
    assert out['positive_pct'] == 0.0
