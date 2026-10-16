"""S91 — change point tests."""
from __future__ import annotations

from src.services.cro_anomaly_change_point import (
    detect_change_points, first_change_point, summarise_change,
)


def test_insufficient_data():
    out = detect_change_points([1, 2, 3])
    assert out['change_points'] == []
    assert out['reason'] == 'insufficient_data'


def test_stable_series_no_change_points():
    series = [10, 11, 9, 10, 11, 9, 10, 11, 9, 10]
    out = detect_change_points(series, baseline_window=4)
    assert out['change_points'] == []


def test_upward_jump_detected():
    series = [10, 10, 10, 10, 50, 50, 50, 50]
    out = detect_change_points(series, baseline_window=4, h_ratio=2.0)
    assert any(p['direction'] == 'up' for p in out['change_points'])


def test_downward_drop_detected():
    series = [50, 50, 50, 50, 10, 10, 10, 10]
    out = detect_change_points(series, baseline_window=4, h_ratio=2.0)
    assert any(p['direction'] == 'down' for p in out['change_points'])


def test_first_change_point_returns_first():
    series = [10, 10, 10, 10, 50, 50, 50, 50]
    out = detect_change_points(series, baseline_window=4, h_ratio=2.0)
    fp = first_change_point(out)
    assert fp is not None
    assert fp['direction'] == 'up'


def test_first_change_point_none_when_no_change():
    out = detect_change_points([10] * 10, baseline_window=4)
    assert first_change_point(out) is None


def test_summarise_no_change():
    assert '稳定' in summarise_change({'change_points': []})


def test_summarise_with_change():
    s = summarise_change({'change_points': [
        {'index': 5, 'direction': 'down', 'cusum': 12.3},
    ]})
    assert '↓' in s
    assert 'index=5' in s


def test_zero_variance_baseline_does_not_crash():
    # constant baseline, then jump
    series = [10, 10, 10, 10, 100, 100, 100]
    out = detect_change_points(series, baseline_window=4, h_ratio=1.0)
    assert isinstance(out, dict)
