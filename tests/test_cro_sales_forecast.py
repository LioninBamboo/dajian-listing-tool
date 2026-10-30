"""S67 — sales forecast tests."""
from __future__ import annotations

from src.services.cro_sales_forecast import (
    days_to_stockout, forecast_holt, forecast_simple_avg, forecast_summary,
)


def test_holt_short_history_falls_back_to_average():
    fc = forecast_holt([1, 2, 3], horizon=5)
    assert len(fc) == 5
    assert all(x == 2.0 for x in fc)


def test_holt_constant_series_predicts_constant():
    fc = forecast_holt([5] * 14, horizon=7)
    assert len(fc) == 7
    assert all(abs(x - 5.0) < 1e-6 for x in fc)


def test_holt_growing_series_predicts_growing():
    fc = forecast_holt([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], horizon=5)
    assert fc[0] < fc[-1]
    assert fc[-1] > 10  # 趋势外推
    assert all(fc[i] <= fc[i + 1] for i in range(len(fc) - 1))


def test_holt_no_negative_predictions():
    fc = forecast_holt([10, 8, 6, 4, 2, 1, 0, 0, 0, 0], horizon=10)
    assert all(x >= 0 for x in fc)


def test_simple_avg_basic():
    assert forecast_simple_avg([2, 4], horizon=3) == [3.0, 3.0, 3.0]
    assert forecast_simple_avg([], horizon=3) == [0.0, 0.0, 0.0]


def test_days_to_stockout_zero_stock():
    assert days_to_stockout(0, [1, 1, 1]) == 0


def test_days_to_stockout_normal():
    # 5 件库存, 每天卖 2 件: 第 3 天断货 (cum=6>=5)
    assert days_to_stockout(5, [2, 2, 2, 2]) == 3


def test_days_to_stockout_never_in_horizon():
    assert days_to_stockout(100, [1, 1, 1]) is None


def test_forecast_summary_structure():
    out = forecast_summary([5] * 14, stock_units=20, horizon=7)
    assert out['method'] == 'holt'
    assert out['horizon'] == 7
    assert len(out['forecast']) == 7
    assert out['avg_daily_demand'] > 0
    assert out['days_to_stockout'] == 4  # cum 5,10,15,20


def test_forecast_summary_short_history_uses_fallback():
    out = forecast_summary([1, 2, 3], stock_units=10, horizon=5)
    assert out['method'] == 'fallback_avg'
    assert out['avg_daily_demand'] == 2.0
