"""S83 — cohort drift tests."""
from __future__ import annotations

from src.services.cro_cohort_drift import (
    annotate_lift_with_drift, detect_drift, summarise_drift,
)


def test_insufficient_history():
    out = detect_drift([{'week': 'W1', 'control_value': 100}])
    assert out['drift_detected'] is False
    assert out['reason'] == 'insufficient_history'


def test_no_drift_within_threshold():
    rows = [{'week': 'W1', 'control_value': 100},
            {'week': 'W2', 'control_value': 105}]
    out = detect_drift(rows, threshold=0.20)
    assert out['drift_detected'] is False
    assert out['changes'][0]['drift'] is False


def test_drift_detected_above_threshold():
    rows = [{'week': 'W1', 'control_value': 100},
            {'week': 'W2', 'control_value': 130}]
    out = detect_drift(rows, threshold=0.20)
    assert out['drift_detected'] is True
    assert out['drift_weeks'] == ['W2']


def test_drift_negative_direction():
    rows = [{'week': 'W1', 'control_value': 100},
            {'week': 'W2', 'control_value': 70}]
    out = detect_drift(rows, threshold=0.20)
    assert out['drift_detected'] is True


def test_drift_zero_previous_handled():
    rows = [{'week': 'W1', 'control_value': 0},
            {'week': 'W2', 'control_value': 50}]
    out = detect_drift(rows)
    assert out['changes'][0]['pct_change'] == 0.0


def test_unsorted_input_sorted_internally():
    rows = [{'week': 'W2', 'control_value': 130},
            {'week': 'W1', 'control_value': 100}]
    out = detect_drift(rows, threshold=0.20)
    assert out['changes'][0]['week'] == 'W2'


def test_skips_rows_missing_metric():
    rows = [{'week': 'W1', 'control_value': 100},
            {'week': 'W2'},
            {'week': 'W3', 'control_value': 150}]
    out = detect_drift(rows, threshold=0.20)
    # only W1->W3 considered
    assert len(out['changes']) == 1


def test_annotate_lift_with_drift_marks_weeks():
    weekly = [{'week': 'W1', 'lift_pct': 0.05},
              {'week': 'W2', 'lift_pct': 0.10}]
    drift = {'drift_weeks': ['W2']}
    out = annotate_lift_with_drift(weekly, drift)
    assert out[0]['control_drift'] is False
    assert out[1]['control_drift'] is True


def test_summarise_no_drift():
    s = summarise_drift({'drift_detected': False})
    assert '稳定' in s


def test_summarise_with_drift():
    s = summarise_drift({'drift_detected': True, 'drift_weeks': ['W2', 'W4']})
    assert 'W2' in s and 'W4' in s
