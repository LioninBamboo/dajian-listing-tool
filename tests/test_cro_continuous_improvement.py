"""S140 — continuous improvement tests."""
from __future__ import annotations

from src.services.cro_continuous_improvement import (
    collect_signals, generate_improvements, generate_suggestions,
    rank_suggestions, render_brief, render_weekly_brief,
)


def test_no_fetchers_yields_no_suggestions():
    out = generate_improvements()
    assert out['count'] == 0
    assert out['suggestions'] == []


def test_collect_signals_swallows_failures():
    out = collect_signals(
        traffic_light_fn=lambda: {'red_pillars': ['pricing']},
        returns_fn=lambda: (_ for _ in ()).throw(RuntimeError('boom')),
        lift_fn=lambda: {'lift_trend': 'down'},
    )
    assert out['traffic_light']['red_pillars'] == ['pricing']
    assert out['returns'] is None
    assert out['lift']['lift_trend'] == 'down'


def test_low_improved_rate_triggers_relax():
    out = generate_improvements(
        effect_audit_fetcher=lambda: {'improved_rate': 0.20, 'total': 50},
    )
    assert any('Relax' in s['recommendation'] for s in out['suggestions'])


def test_high_improved_rate_triggers_tighten():
    out = generate_improvements(
        effect_audit_fetcher=lambda: {'improved_rate': 0.80, 'total': 50},
    )
    assert any('Tighten' in s['recommendation'] for s in out['suggestions'])


def test_low_total_does_not_trigger():
    out = generate_improvements(
        effect_audit_fetcher=lambda: {'improved_rate': 0.10, 'total': 5},
    )
    assert out['count'] == 0


def test_sentinel_critical_high_priority():
    out = generate_improvements(
        sentinel_stats_fetcher=lambda: {'critical_count': 8,
                                          'recurring_skus': 4},
    )
    priorities = {s['priority'] for s in out['suggestions']}
    assert 'critical' in priorities
    assert 'high' in priorities


def test_approvals_pending_too_many():
    out = generate_improvements(
        approvals_stats_fetcher=lambda: {'pending_count': 100,
                                           'avg_age_days': 10.5},
    )
    assert out['count'] == 2


def test_sales_health_zero_sale_skus():
    out = generate_improvements(
        sales_health_fetcher=lambda: {'declining_skus': 15,
                                        'zero_sale_skus': 30},
    )
    assert out['count'] == 2
    assert out['by_area']['sales_health'] == 2


def test_priority_sort_critical_first():
    out = generate_improvements(
        sentinel_stats_fetcher=lambda: {'critical_count': 10},
        effect_audit_fetcher=lambda: {'improved_rate': 0.20, 'total': 50},
    )
    assert out['suggestions'][0]['priority'] == 'critical'


def test_generate_suggestions_uses_signal_contract():
    suggestions = generate_suggestions({
        'traffic_light': {'red_pillars': ['pricing']},
        'returns': {'rate': 0.15},
        'lift': {'lift_trend': 'down'},
        'cost': {'over_budget': True},
    })
    areas = {item['area'] for item in suggestions}
    assert {'traffic_light', 'returns', 'lift', 'cost'} <= areas


def test_rank_suggestions_orders_by_severity_then_evidence():
    ranked = rank_suggestions([
        {'area': 'a', 'severity': 'high', 'evidence_strength': 1},
        {'area': 'b', 'severity': 'critical', 'evidence_strength': 1},
        {'area': 'c', 'severity': 'high', 'evidence_strength': 5},
    ])
    assert [item['area'] for item in ranked] == ['b', 'c', 'a']


def test_failing_fetcher_does_not_crash():
    def boom():
        raise RuntimeError('x')
    out = generate_improvements(effect_audit_fetcher=boom,
                                  sentinel_stats_fetcher=boom,
                                  approvals_stats_fetcher=boom,
                                  sales_health_fetcher=boom)
    assert out['count'] == 0


def test_render_brief_empty_state():
    out = generate_improvements()
    md = render_brief(out)
    assert 'No actionable improvements' in md


def test_render_brief_with_suggestions():
    out = generate_improvements(
        sentinel_stats_fetcher=lambda: {'critical_count': 8})
    md = render_brief(out)
    assert '[critical]' in md
    assert 'monitoring' in md


def test_render_weekly_brief_falls_back_when_llm_fails():
    md = render_weekly_brief([
        {'area': 'traffic_light', 'severity': 'critical',
         'action': 'fix red pillar', 'evidence': 'pricing red',
         'evidence_strength': 3},
    ], llm_call=lambda _: (_ for _ in ()).throw(RuntimeError('boom')))
    assert 'Weekly Improvement Brief' in md
    assert 'fix red pillar' in md
