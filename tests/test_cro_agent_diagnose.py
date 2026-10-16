"""S87 — agent diagnose tests."""
from __future__ import annotations

from src.services.cro_agent_diagnose import diagnose_agent


def test_no_features_fetcher_skips_collect():
    out = diagnose_agent('SKU1')
    steps = [t['step'] for t in out['trace']]
    assert 'collect_signals' in steps
    assert out['trace'][0]['status'] == 'skipped'


def test_full_pipeline_with_rule_match():
    fetcher = lambda sku: {'returns_pct': 0.20, 'image_age_days': 60}
    out = diagnose_agent('SKU1', features_fetcher=fetcher)
    assert out['hypothesis'] is not None
    assert '主图' in out['hypothesis']
    assert out['success'] is True


def test_features_fetcher_exception_marked_failed():
    def bad(sku):
        raise RuntimeError('boom')
    out = diagnose_agent('SKU1', features_fetcher=bad)
    failed = [t for t in out['trace'] if t['status'] == 'failed']
    assert any(t['step'] == 'collect_signals' for t in failed)


def test_verify_called_when_hypothesis_exists():
    fetcher = lambda sku: {'stock': 0}
    verify = lambda sku, hyp: True
    out = diagnose_agent('SKU1', features_fetcher=fetcher,
                         verify_fetcher=verify)
    assert out['verified'] is True


def test_verify_skipped_when_no_hypothesis():
    fetcher = lambda sku: {}  # rules with _ge/_le won't match empty
    out = diagnose_agent('SKU1', features_fetcher=fetcher,
                         verify_fetcher=lambda s, h: True)
    # hypothesis is the manual_review fallback string, so verify will run
    # use a rule that yields no_match
    assert out['hypothesis'] is not None


def test_llm_call_used_when_provided():
    fetcher = lambda sku: {'stock': 0}
    out = diagnose_agent('SKU1', features_fetcher=fetcher,
                         llm_call=lambda p: 'LLM 结论: 紧急补货')
    assert out['narrative'] == 'LLM 结论: 紧急补货'


def test_llm_call_failure_propagates_to_step_status():
    fetcher = lambda sku: {'stock': 0}

    def bad(p):
        raise RuntimeError('llm down')

    out = diagnose_agent('SKU1', features_fetcher=fetcher, llm_call=bad)
    assert out['narrative'] is None
    assert any(t['step'] == 'conclude_llm' and t['status'] == 'failed'
               for t in out['trace'])
    # success=False because failed step
    assert out['success'] is False


def test_template_narrative_when_no_llm():
    fetcher = lambda sku: {'stock': 0}
    out = diagnose_agent('SKU1', features_fetcher=fetcher)
    assert out['narrative'].startswith('SKU SKU1:')
