"""S137 — capacity planning tests."""
from __future__ import annotations

from datetime import datetime

from src.services.cro_capacity_planning import (
    assess_components, project_exhaustion,
)


def _samples_growing():
    # day 0..6: 100, 110, 120, 130, 140, 150, 160 (slope=10/day)
    return [{'day_index': i, 'value': 100 + 10 * i} for i in range(7)]


def test_invalid_capacity():
    out = project_exhaustion(samples=_samples_growing(), capacity=0,
                              current_value=160)
    assert out['status'] == 'invalid_capacity'


def test_no_growth():
    flat = [{'day_index': i, 'value': 100} for i in range(5)]
    out = project_exhaustion(samples=flat, capacity=1000, current_value=100)
    assert out['status'] == 'no_growth'


def test_insufficient_data():
    out = project_exhaustion(samples=[], capacity=1000, current_value=10)
    assert out['status'] == 'insufficient_data'


def test_projected_normal():
    out = project_exhaustion(samples=_samples_growing(),
                              capacity=200, current_value=160,
                              now=datetime(2026, 5, 1))
    assert out['status'] == 'projected'
    # remaining 40 / slope 10 = 4 days → critical
    assert out['days_until_full'] == 4.0
    assert out['severity'] == 'critical'


def test_severity_high():
    out = project_exhaustion(samples=_samples_growing(),
                              capacity=300, current_value=160,
                              now=datetime(2026, 5, 1))
    # remaining 140/10=14d → high
    assert out['severity'] == 'high'


def test_severity_medium():
    out = project_exhaustion(samples=_samples_growing(),
                              capacity=600, current_value=160,
                              now=datetime(2026, 5, 1))
    # 440/10 = 44 days → medium
    assert out['severity'] == 'medium'


def test_severity_low():
    out = project_exhaustion(samples=_samples_growing(),
                              capacity=2000, current_value=160,
                              now=datetime(2026, 5, 1))
    assert out['severity'] == 'low'


def test_used_pct_reported():
    out = project_exhaustion(samples=_samples_growing(),
                              capacity=200, current_value=160)
    assert out['used_pct'] == 0.8


def test_assess_components_worst_severity():
    comps = {
        'disk': {'samples': _samples_growing(),
                  'capacity': 200, 'current_value': 160},  # critical
        'db':   {'samples': _samples_growing(),
                  'capacity': 2000, 'current_value': 160},  # low
    }
    out = assess_components(comps, now=datetime(2026, 5, 1))
    assert out['worst_severity'] == 'critical'
    assert 'disk' in out['critical_components']
    assert 'db' not in out['critical_components']


def test_assess_components_empty():
    out = assess_components({})
    assert out['worst_severity'] == 'low'
    assert out['critical_components'] == []
