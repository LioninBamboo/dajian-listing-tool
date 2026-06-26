"""S48 — 退货反馈环 tests."""
from __future__ import annotations

from src.services.cro_returns_feedback import (
    analyze_returns, render_returns_brief,
)


def _fetcher(rows):
    return lambda: rows


def test_high_return_size_mismatch_recommends_dimensions_audit():
    rows = [{'sku': 'A', 'sold_count': 100, 'return_count': 20,
             'reason_codes': ['NOT_AS_DESCRIBED_SIZE'] * 18 + ['OTHER'] * 2}]
    out = analyze_returns(_fetcher(rows))
    assert out['high_return_count'] == 1
    item = out['high_return'][0]
    assert item['reason_category'] == 'size_mismatch'
    assert 'dimensions' in item['recommendation']
    assert 'A' not in out['blacklist_skus']  # size_mismatch 不进黑名单


def test_quality_defect_goes_to_blacklist():
    rows = [{'sku': 'B', 'sold_count': 50, 'return_count': 10,
             'reason_codes': ['DEFECTIVE'] * 10}]
    out = analyze_returns(_fetcher(rows))
    assert 'B' in out['blacklist_skus']


def test_low_sold_volume_skipped():
    rows = [{'sku': 'C', 'sold_count': 5, 'return_count': 5,
             'reason_codes': ['DEFECTIVE'] * 5}]
    out = analyze_returns(_fetcher(rows))
    assert out['high_return'] == []
    assert out['skipped_low_volume'] == 1


def test_normal_return_rate_excluded():
    rows = [{'sku': 'D', 'sold_count': 100, 'return_count': 5,
             'reason_codes': ['DEFECTIVE'] * 5}]
    out = analyze_returns(_fetcher(rows))
    assert out['high_return'] == []


def test_sorted_by_rate_descending():
    rows = [
        {'sku': 'lo', 'sold_count': 100, 'return_count': 16,
         'reason_codes': ['OTHER'] * 16},
        {'sku': 'hi', 'sold_count': 100, 'return_count': 30,
         'reason_codes': ['OTHER'] * 30},
    ]
    out = analyze_returns(_fetcher(rows))
    assert out['high_return'][0]['sku'] == 'hi'


def test_render_brief_smoke():
    rows = [{'sku': 'X', 'sold_count': 50, 'return_count': 15,
             'reason_codes': ['DEFECTIVE'] * 15}]
    out = analyze_returns(_fetcher(rows))
    text = render_returns_brief(out)
    assert 'X' in text
    assert '黑名单' in text or '供应商' in text


def test_render_brief_empty():
    assert '正常' in render_returns_brief({'high_return': []})
