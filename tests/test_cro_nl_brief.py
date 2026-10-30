"""S39 \u2014 NL brief tests."""
from __future__ import annotations

from src.services.cro_nl_brief import (
    build_prompt, fallback_summary, summarize_with_llm,
)


def _sample():
    return {
        'summary': {'total_evaluated': 30, 'improved': 12, 'flat': 14,
                    'worsened': 4},
        'suggestions': [
            {'category_id': 'CAT-A', 'metric': 'ctr', 'suggested': 0.018,
             'direction': 'relax'},
            {'category_id': 'CAT-B', 'metric': 'cvr', 'suggested': 0.025,
             'direction': 'tighten'},
        ],
        'rollback_candidates': [
            {'sku': 'X', 'roi': 0.4},
        ],
        'sentinel_alerts': [],
    }


def test_build_prompt_includes_counts_and_blocks():
    p = build_prompt(_sample())
    assert '\u8bc4\u4f30\u52a8\u4f5c 30 \u4e2a' in p
    assert 'improved 12' in p
    assert 'CAT-A' in p


def test_summarize_with_llm_calls_callable():
    captured = {}
    def llm(prompt):
        captured['p'] = prompt
        return '## \u603b\u7ed3\n\u4e00\u5207\u826f\u597d'
    out = summarize_with_llm(_sample(), llm)
    assert '\u4e00\u5207\u826f\u597d' in out
    assert 'CAT-A' in captured['p']


def test_summarize_returns_failure_message_on_exception():
    def bad(p):
        raise RuntimeError('api timeout')
    out = summarize_with_llm(_sample(), bad)
    assert '\u751f\u6210\u5931\u8d25' in out and 'api timeout' in out


def test_summarize_returns_placeholder_on_empty():
    out = summarize_with_llm(_sample(), lambda p: '   ')
    assert '\u4e3a\u7a7a' in out


def test_fallback_summary_smoke():
    out = fallback_summary(_sample())
    assert '\u4e00\u53e5\u8bdd\u603b\u7ed3' in out
    assert 'CAT-A' in out
    assert 'X' in out
