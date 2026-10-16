"""S101 — VoC topic modeling tests."""
from __future__ import annotations

from src.services.cro_voc_topic_modeling import (
    compute_tfidf, extract_topics, summarise_topics, tokenize,
)


def test_tokenize_filters_stopwords_and_short():
    out = tokenize('The quality is excellent and the size is wrong')
    assert 'the' not in out
    assert 'is' not in out
    assert 'quality' in out
    assert 'excellent' in out


def test_tokenize_handles_chinese():
    out = tokenize('质量 很好 但 尺寸 不对')
    assert '质量' in out
    assert '尺寸' in out


def test_tokenize_empty():
    assert tokenize('') == []
    assert tokenize(None) == []


def test_tfidf_empty():
    assert compute_tfidf([]) == []


def test_tfidf_basic():
    docs = [['quality', 'good'], ['quality', 'bad'], ['shipping', 'slow']]
    tfidf = compute_tfidf(docs)
    assert len(tfidf) == 3
    # 'quality' 出现在 2/3 doc → idf 较低；'shipping' 出现在 1/3 → idf 较高
    assert tfidf[2]['shipping'] > tfidf[0]['quality']


def test_extract_topics_empty():
    out = extract_topics([])
    assert out['topics'] == []
    assert out['doc_count'] == 0


def test_extract_topics_groups_similar():
    reviews = [
        'great quality product',
        'excellent quality build',
        'wrong size shipped',
        'size too small',
        'shipping was slow shipping',
    ]
    out = extract_topics(reviews, max_topics=5, min_topic_size=2)
    primaries = [t['primary_keyword'] for t in out['topics']]
    assert 'quality' in primaries or 'size' in primaries or 'shipping' in primaries
    assert out['doc_count'] == 5


def test_extract_topics_filters_small_topics():
    reviews = ['unique singleton review']  # 1 doc → below min_topic_size=2
    out = extract_topics(reviews, min_topic_size=2)
    assert out['topics'] == []


def test_summarise_topics_text():
    out = extract_topics(['quality good', 'quality bad', 'quality fine'])
    text = summarise_topics(out)
    assert 'quality' in text


def test_summarise_topics_empty():
    assert '无主题' in summarise_topics({'topics': []})
