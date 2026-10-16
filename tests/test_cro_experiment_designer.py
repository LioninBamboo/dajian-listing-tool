"""S45 \u2014 \u5b9e\u9a8c\u8bbe\u8ba1\u5668 tests."""
from __future__ import annotations

import pytest

from src.services.cro_experiment_designer import (
    design_experiment, required_sample_per_arm,
)


def test_low_baseline_needs_large_sample():
    n_low = required_sample_per_arm(0.01, mde_relative=0.20)
    n_high = required_sample_per_arm(0.10, mde_relative=0.20)
    assert n_low > n_high
    assert n_low > 1000


def test_larger_mde_smaller_sample():
    n_small = required_sample_per_arm(0.05, mde_relative=0.10)
    n_large = required_sample_per_arm(0.05, mde_relative=0.50)
    assert n_large < n_small


def test_invalid_baseline_raises():
    with pytest.raises(ValueError):
        required_sample_per_arm(0, 0.2)
    with pytest.raises(ValueError):
        required_sample_per_arm(1.0, 0.2)


def test_invalid_mde_raises():
    with pytest.raises(ValueError):
        required_sample_per_arm(0.05, 0)


def test_design_experiment_feasible():
    d = design_experiment('title-fs', baseline_cvr=0.10, mde_relative=0.50,
                          daily_conversions=500, max_days=28)
    assert d['feasible']
    assert d['days_committed'] <= 28
    assert d['control_ratio'] == 0.20
    assert d['treatment_ratio'] == 0.80


def test_design_experiment_infeasible_low_traffic():
    d = design_experiment('niche', baseline_cvr=0.01, mde_relative=0.10,
                          daily_conversions=2, max_days=14)
    assert not d['feasible']
    assert '\u9700' in d['notes']


def test_stop_conditions_present():
    d = design_experiment('exp', 0.05, 0.20, 100, max_days=21,
                          negative_guard_drop_pct=0.30)
    sc = d['stop_conditions']
    assert sc['on_negative_drop_pct_below'] == -0.30
    assert sc['on_max_days'] == 21
    assert sc['on_treatment_n_reached'] == d['sample_per_arm_required']
