"""S75 — seasonality detection tests."""
from __future__ import annotations

import math

from src.services.cro_seasonality_detect import (
    detect_seasonality, fft_dominant_period,
)


def _sine(period, n=84, amp=10, base=20):
    return [base + amp * math.sin(2 * math.pi * t / period) for t in range(n)]


def test_constant_series_no_period():
    out = detect_seasonality([5] * 60)
    assert out['period_label'] == 'no_clear_period'
    assert out['best_lag'] is None


def test_weekly_period_detected():
    # 7-day repeating pattern over 84 days
    pattern = [1, 5, 2, 8, 3, 9, 6]
    series = pattern * 12
    out = detect_seasonality(series, lags=(7, 14, 30))
    rows = {r['lag']: r for r in out['rows']}
    assert rows[7]['significant']
    assert out['best_lag'] == 7


def test_30day_pattern_strong_at_30():
    pattern = list(range(30))
    series = pattern * 4
    out = detect_seasonality(series, lags=(7, 14, 30))
    assert out['best_lag'] == 30


def test_short_series_lags_too_long():
    out = detect_seasonality([1, 2, 3], lags=(7, 14))
    # all autocorr None
    assert all(r['autocorr'] is None for r in out['rows'])
    assert out['period_label'] == 'no_clear_period'


def test_fft_dominant_period_basic():
    series = _sine(period=10, n=80)
    p = fft_dominant_period(series)
    # 容忍 ±1 (整数 round)
    assert p is not None
    assert abs(p - 10) <= 1


def test_fft_too_short_returns_none():
    assert fft_dominant_period([1, 2]) is None


def test_fft_max_period_cap():
    series = _sine(period=20, n=100)
    p = fft_dominant_period(series, max_period=10)
    # capped → can't return 20
    assert p is None or p <= 10
