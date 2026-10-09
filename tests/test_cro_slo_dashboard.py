"""S90 — SLO dashboard tests."""
from __future__ import annotations

from src.services.cro_slo_dashboard import (
    compute_error_budget, compute_sli, freeze_recommendation, slo_dashboard,
)


def test_compute_sli_empty_returns_perfect():
    out = compute_sli([])
    assert out == {'total': 0, 'success': 0, 'failure': 0, 'sli': 1.0}


def test_compute_sli_basic():
    events = [{'success': True}, {'success': False}, {'success': True}]
    out = compute_sli(events)
    assert out == {'total': 3, 'success': 2, 'failure': 1,
                   'sli': round(2 / 3, 6)}


def test_compute_error_budget_under_budget():
    sli = {'total': 1000, 'success': 995, 'failure': 5}
    bud = compute_error_budget(sli, slo=0.99)
    assert bud['budget_total'] == 10.0
    assert bud['budget_consumed'] == 5
    assert bud['budget_remaining'] == 5.0
    assert bud['over_budget'] is False
    assert bud['budget_remaining_pct'] == 0.5


def test_compute_error_budget_over_budget():
    sli = {'total': 1000, 'success': 980, 'failure': 20}
    bud = compute_error_budget(sli, slo=0.99)
    assert bud['over_budget'] is True
    assert bud['budget_remaining'] == 0.0


def test_compute_error_budget_no_events():
    bud = compute_error_budget({'total': 0, 'failure': 0}, slo=0.99)
    assert bud['over_budget'] is False
    assert bud['budget_total'] == 0


def test_freeze_recommendation_red_when_over():
    out = freeze_recommendation({'over_budget': True,
                                  'budget_remaining_pct': 0.0})
    assert out['freeze'] is True
    assert out['severity'] == 'red'


def test_freeze_recommendation_red_when_below_10pct():
    out = freeze_recommendation({'over_budget': False,
                                  'budget_remaining_pct': 0.05})
    assert out['freeze'] is True
    assert out['severity'] == 'red'


def test_freeze_recommendation_yellow_below_25pct():
    out = freeze_recommendation({'over_budget': False,
                                  'budget_remaining_pct': 0.20})
    assert out['freeze'] is False
    assert out['severity'] == 'yellow'


def test_freeze_recommendation_green_healthy():
    out = freeze_recommendation({'over_budget': False,
                                  'budget_remaining_pct': 0.80})
    assert out['severity'] == 'green'


def test_slo_dashboard_full_pipeline(tmp_path):
    log = tmp_path / 's.jsonl'
    lines = [
        '{"success": true}',
        '{"success": true}',
        '{"success": false}',
        'not json',
    ]
    log.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    out = slo_dashboard(log_path=log, slo=0.99)
    assert out['sli']['total'] == 3
    assert out['error_budget']['slo'] == 0.99
    assert 'recommendation' in out


def test_slo_dashboard_empty_log_clean(tmp_path):
    out = slo_dashboard(log_path=tmp_path / 'none.jsonl')
    assert out['sli']['total'] == 0
    assert out['recommendation']['severity'] == 'green'
