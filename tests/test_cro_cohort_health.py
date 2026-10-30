"""S77 — cohort health tests."""
from __future__ import annotations

from src.services.cro_cohort_health import (
    compute_weekly_lift, health_report, should_stop_ab, trend,
)


def _wk(week, control, treatment):
    return [
        {'week': week, 'cohort': 'control', 'sold_qty': control},
        {'week': week, 'cohort': 'treatment', 'sold_qty': treatment},
    ]


def test_compute_weekly_lift_basic():
    rows = _wk('W1', 100, 110)  # +10%
    out = compute_weekly_lift(rows)
    assert out == [{'week': 'W1', 'control': 100, 'treatment': 110,
                    'lift_pct': 0.1}]


def test_compute_weekly_lift_zero_control_returns_none():
    rows = _wk('W1', 0, 50)
    assert compute_weekly_lift(rows)[0]['lift_pct'] is None


def test_compute_weekly_lift_sums_within_cohort():
    rows = [
        {'week': 'W1', 'cohort': 'control', 'sold_qty': 50},
        {'week': 'W1', 'cohort': 'control', 'sold_qty': 50},
        {'week': 'W1', 'cohort': 'treatment', 'sold_qty': 110},
    ]
    out = compute_weekly_lift(rows)
    assert out[0]['control'] == 100
    assert out[0]['lift_pct'] == 0.1


def test_trend_improving():
    weekly = [{'lift_pct': 0.0}, {'lift_pct': 0.10}]
    assert trend(weekly) == 'improving'


def test_trend_worsening():
    weekly = [{'lift_pct': 0.0}, {'lift_pct': -0.10}]
    assert trend(weekly) == 'worsening'


def test_trend_flat():
    weekly = [{'lift_pct': 0.01}, {'lift_pct': -0.02}]
    assert trend(weekly) == 'flat'


def test_trend_unknown_when_no_data():
    assert trend([]) == 'unknown'
    assert trend([{'lift_pct': None}]) == 'unknown'


def test_should_stop_ab_insufficient_data():
    out = should_stop_ab([{'lift_pct': -0.10}])
    assert out['stop'] is False
    assert out['reason'] == 'insufficient_data'


def test_should_stop_ab_three_negative_weeks_triggers():
    weekly = [{'lift_pct': -0.10}, {'lift_pct': -0.08},
              {'lift_pct': -0.12}]
    out = should_stop_ab(weekly)
    assert out['stop'] is True


def test_should_stop_ab_one_positive_breaks_streak():
    weekly = [{'lift_pct': -0.10}, {'lift_pct': 0.05},
              {'lift_pct': -0.10}]
    out = should_stop_ab(weekly)
    assert out['stop'] is False


def test_health_report_aggregates():
    rows = (_wk('W1', 100, 90) + _wk('W2', 100, 85)
            + _wk('W3', 100, 88))
    out = health_report(rows)
    assert out['trend'] == 'worsening'
    assert out['stop']['stop'] is True
    assert out['metric'] == 'sold_qty'
