"""S84 — returns NLP tests."""
from __future__ import annotations

from src.services.cro_returns_nlp import (
    classify_batch, classify_reason, classify_with_keywords, classify_with_llm,
)


def test_keyword_size_en():
    assert classify_with_keywords('size too small') == 'size'


def test_keyword_size_cn():
    assert classify_with_keywords('尺寸太小了') == 'size'


def test_keyword_quality_cn():
    assert classify_with_keywords('收到就坏了, 质量太差') == 'quality'


def test_keyword_color():
    assert classify_with_keywords('color is different') == 'color'


def test_keyword_wrong_item_cn():
    assert classify_with_keywords('发错货了') == 'wrong_item'


def test_keyword_other_when_no_match():
    assert classify_with_keywords('不喜欢') == 'other'


def test_keyword_handles_none_or_nonstring():
    assert classify_with_keywords('') == 'other'
    assert classify_with_keywords(None) == 'other'


def test_classify_reason_keyword_source():
    out = classify_reason('size too small')
    assert out == {'bucket': 'size', 'source': 'keyword',
                   'text': 'size too small'}


def test_classify_reason_llm_used_when_keyword_other():
    out = classify_reason('I just hate it', llm_call=lambda p: 'quality')
    assert out['bucket'] == 'quality'
    assert out['source'] == 'llm'


def test_classify_reason_llm_returns_unknown_falls_back_to_other():
    out = classify_reason('whatever', llm_call=lambda p: 'maybe')
    assert out['bucket'] == 'other'
    assert out['source'] == 'keyword'


def test_classify_with_llm_exception_returns_other():
    def bad(p):
        raise RuntimeError('api')

    assert classify_with_llm('hi', bad) == 'other'


def test_classify_batch_aggregates():
    texts = ['size too small', '尺寸不对', '质量差', 'random complaint']
    out = classify_batch(texts)
    assert out['total'] == 4
    assert out['by_bucket']['size'] == 2
    assert out['by_bucket']['quality'] == 1
    assert out['by_bucket']['other'] == 1
    assert out['top_bucket'] == 'size'


def test_classify_batch_empty():
    out = classify_batch([])
    assert out['total'] == 0
    assert out['top_bucket'] is None
