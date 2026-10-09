"""S102 — long-term lift decay tests."""
from __future__ import annotations

import math

from src.services.cro_long_term_lift import (
    batch_fit, fit_decay, is_decay_complete, project_future_lift,
)


def _exp_series(L0: float, k: float, n: int):
    return [{'period_index': t, 'lift_pct': L0 * math.exp(-k * t)}
            for t in range(n)]


def test_fit_empty():
    assert fit_decay([])['status'] == 'empty'


def test_fit_single_point_cannot_fit():
    out = fit_decay([{'period_index': 0, 'lift_pct': 0.5}])
    assert out['status'] == 'cannot_fit'


def test_fit_recovers_known_decay_rate():
    series = _exp_series(0.5, 0.10, 10)
    out = fit_decay(series)
    assert out['status'] == 'fit_ok'
    assert abs(out['k'] - 0.10) < 1e-3
    expected_half = math.log(2) / 0.10
    assert abs(out['half_life_periods'] - expected_half) < 0.05


def test_fit_no_decay_when_increasing():
    series = [{'period_index': i, 'lift_pct': 0.1 * (i + 1)} for i in range(5)]
    out = fit_decay(series)
    assert out['status'] == 'no_decay'


def test_is_decay_complete_below_threshold():
    series = _exp_series(0.5, 0.5, 12)
    out = fit_decay(series)
    assert is_decay_complete(out, threshold=0.05) is True


def test_is_decay_not_complete_when_residual_high():
    series = _exp_series(0.5, 0.05, 5)
    out = fit_decay(series)
    assert is_decay_complete(out, threshold=0.05) is False


def test_project_future_lift_decreases_over_time():
    series = _exp_series(0.5, 0.10, 5)
    out = fit_decay(series)
    near = project_future_lift(out, 1)
    far = project_future_lift(out, 20)
    assert near > far


def test_project_returns_none_when_no_fit():
    assert project_future_lift({'status': 'empty'}, 5) is None


def test_batch_fit_aggregates_decayed_count():
    rows = [
        {'sku': 'A', 'series': _exp_series(0.5, 0.5, 12)},   # 衰减完
        {'sku': 'B', 'series': _exp_series(0.5, 0.05, 5)},   # 还没
        {'sku': 'C', 'series': []},                           # 空
    ]
    out = batch_fit(rows)
    assert out['count'] == 3
    assert out['decayed_count'] == 1
