"""S59 — queue trace integration tests."""
from __future__ import annotations

import json

from src.services.cro_queue_with_trace import (
    enqueue_with_trace, read_traces_for_pending, render_trace_text,
)


def test_enqueue_writes_trace(tmp_path):
    qp = tmp_path / 'q.jsonl'
    n = enqueue_with_trace(
        [{'sku': 'A', 'action': 'price_drop',
          'signals': {'funnel': 'low_ctr', 'roi': 0.4}}],
        queue_path=qp,
    )
    assert n == 1
    row = json.loads(qp.read_text(encoding='utf-8').splitlines()[0])
    assert row['reasoning_trace'] == ['funnel:low_ctr', 'roi:0.40']
    assert 'funnel' in row['reasoning_summary']


def test_signal_lookup_callable(tmp_path):
    qp = tmp_path / 'q.jsonl'
    enqueue_with_trace(
        [{'sku': 'A', 'action': 'promote'}],
        signal_lookup=lambda a: {'external': 'macro_drop'},
        queue_path=qp,
    )
    row = json.loads(qp.read_text(encoding='utf-8').splitlines()[0])
    assert row['reasoning_trace'] == ['external:macro_drop']


def test_no_signals_yields_empty_trace(tmp_path):
    qp = tmp_path / 'q.jsonl'
    enqueue_with_trace([{'sku': 'A', 'action': 'promote'}], queue_path=qp)
    row = json.loads(qp.read_text(encoding='utf-8').splitlines()[0])
    assert row['reasoning_trace'] == []
    assert '无显式信号' in row['reasoning_summary']


def test_read_traces_for_pending(tmp_path):
    qp = tmp_path / 'q.jsonl'
    # promote 是 A/B 动作: 不显式钉 cohort 时按 (sku|action|周) 哈希分组,
    # 某些 ISO 周 'B'/'promote' 会落 control 被 load_pending 隐藏 → 按周偶发.
    enqueue_with_trace(
        [{'sku': 'A', 'action': 'price_drop',
          'signals': {'funnel': 'low_ctr'}},
         {'sku': 'B', 'action': 'promote', 'cohort': 'treatment',
          'signals': {'roi': 0.4}}],
        queue_path=qp,
    )
    rows = read_traces_for_pending(queue_path=qp)
    assert len(rows) == 2
    skus = {r['sku'] for r in rows}
    assert skus == {'A', 'B'}


def test_render_trace_text_truncates(tmp_path):
    rows = [{'sku': f's{i}', 'action': 'promote',
             'reasoning_summary': 'x'} for i in range(50)]
    text = render_trace_text(rows, max_lines=5)
    assert text.count('\n') == 4  # 5 lines = 4 \n


def test_render_trace_text_empty():
    assert render_trace_text([]) == '队列空'
