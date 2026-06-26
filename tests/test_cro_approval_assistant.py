"""S42 \u2014 \u5ba1\u6279\u52a9\u624b tests."""
from __future__ import annotations

from src.services.cro_approval_assistant import annotate, annotate_report


def test_promote_rollback_low_roi_auto_approve():
    items = [{'kind': 'promote_rollback', 'sku': 'X',
              'summary': 'ROI=0.4 lift=1 spend=100'}]
    out = annotate(items)
    assert out[0]['suggestion']['verdict'] == 'approve'
    assert '0.4' in out[0]['suggestion']['reason']


def test_promote_rollback_borderline_review():
    items = [{'kind': 'promote_rollback', 'sku': 'Y',
              'summary': 'ROI=0.7 lift=2 spend=50'}]
    out = annotate(items)
    assert out[0]['suggestion']['verdict'] == 'review'


def test_threshold_high_samples_approves():
    items = [{'kind': 'threshold_promote', 'category_id': 'C',
              'summary': 'CTR=0.02 (n=50)'}]
    out = annotate(items)
    assert out[0]['suggestion']['verdict'] == 'approve'


def test_threshold_low_samples_waits():
    items = [{'kind': 'threshold_promote', 'category_id': 'C',
              'summary': 'CTR=0.02 (n=5)'}]
    out = annotate(items)
    assert out[0]['suggestion']['verdict'] == 'wait'


def test_delist_always_review():
    items = [{'kind': 'delist', 'sku': 'D'}]
    out = annotate(items)
    assert out[0]['suggestion']['verdict'] == 'review'


def test_unknown_kind_defaults_to_review():
    items = [{'kind': 'mystery'}]
    out = annotate(items)
    assert out[0]['suggestion']['verdict'] == 'review'


def test_llm_call_attaches_note_and_handles_error():
    items = [{'kind': 'delist', 'sku': 'A'}]
    out = annotate(items, llm_call=lambda i: 'good')
    assert out[0]['suggestion']['llm_note'] == 'good'

    def bad(i):
        raise RuntimeError('x')
    out2 = annotate(items, llm_call=bad)
    assert 'llm error' in out2[0]['suggestion']['llm_note']


def test_annotate_report_counts_auto_approve():
    rep = {'items': [
        {'kind': 'promote_rollback', 'sku': 'A', 'summary': 'ROI=0.3'},
        {'kind': 'delist', 'sku': 'B'},
        {'kind': 'threshold_promote', 'category_id': 'C',
         'summary': 'CTR (n=100)'},
    ]}
    out = annotate_report(rep)
    assert out['auto_approve_count'] == 2  # rollback + threshold
