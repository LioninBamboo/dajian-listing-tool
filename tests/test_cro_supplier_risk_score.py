"""S115 — supplier risk score tests."""
from __future__ import annotations

from src.services.cro_supplier_risk_score import (
    batch_evaluate, compute_supplier_metrics, evaluate_supplier,
    label_for_score,
)


def test_metrics_zero_when_no_orders():
    m = compute_supplier_metrics({})
    assert m['stockout_rate'] == 0.0
    assert m['late_rate'] == 0.0
    assert m['return_rate'] == 0.0
    assert m['price_volatility'] == 0.0


def test_metrics_clamped_to_one():
    m = compute_supplier_metrics(
        {'orders': 10, 'stockouts': 100, 'late_shipments': 100,
         'sold_30d': 1, 'returned_30d': 100, 'price_history': []})
    assert m['stockout_rate'] == 1.0
    assert m['return_rate'] == 1.0


def test_price_volatility_zero_when_constant_price():
    m = compute_supplier_metrics({'price_history': [10, 10, 10, 10]})
    assert m['price_volatility'] == 0.0


def test_price_volatility_positive_when_varies():
    m = compute_supplier_metrics({'price_history': [10, 5, 15, 8, 12]})
    assert m['price_volatility'] > 0


def test_label_thresholds():
    assert label_for_score(80) == 'critical'
    assert label_for_score(60) == 'high'
    assert label_for_score(30) == 'medium'
    assert label_for_score(10) == 'low'


def test_evaluate_low_risk_supplier():
    record = {'supplier_id': 'S1', 'name': 'Good Co',
              'orders': 100, 'stockouts': 1, 'late_shipments': 1,
              'sold_30d': 100, 'returned_30d': 1,
              'price_history': [10, 10, 10]}
    out = evaluate_supplier(record)
    assert out['risk_label'] == 'low'
    assert out['risk_score'] < 25
    assert '继续' in out['recommendation'] or '保持' in out['recommendation']


def test_evaluate_critical_risk_supplier():
    record = {'supplier_id': 'S2', 'name': 'Bad Co',
              'orders': 100, 'stockouts': 80, 'late_shipments': 70,
              'sold_30d': 100, 'returned_30d': 60,
              'price_history': [10, 5, 20]}
    out = evaluate_supplier(record)
    assert out['risk_label'] in ('high', 'critical')


def test_batch_evaluate_sorts_and_aggregates():
    records = [
        {'supplier_id': 'A', 'orders': 100, 'stockouts': 80,
         'sold_30d': 100, 'returned_30d': 50},
        {'supplier_id': 'B', 'orders': 100, 'stockouts': 1,
         'sold_30d': 100, 'returned_30d': 1},
    ]
    out = batch_evaluate(records)
    assert out['count'] == 2
    assert out['items'][0]['supplier_id'] == 'A'   # 高风险在前
    assert sum(out['by_label'].values()) == 2
