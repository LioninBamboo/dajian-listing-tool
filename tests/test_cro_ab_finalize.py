"""S133 — A/B finalize tests."""
from __future__ import annotations

from src.services.cro_ab_finalize import (
    archive_experiment, evaluate_significance, finalize_experiment,
)


def test_significance_insufficient_data():
    out = evaluate_significance({'exposures': 0, 'conversions': 0},
                                  {'exposures': 0, 'conversions': 0})
    assert out['significant'] is False
    assert out['reason'] == 'insufficient_data'


def test_significance_positive_lift():
    out = evaluate_significance({'exposures': 1000, 'conversions': 50},
                                  {'exposures': 1000, 'conversions': 100})
    assert out['significant'] is True
    assert out['direction'] == 'positive'
    assert out['lift'] is not None and out['lift'] > 0


def test_significance_negative_lift():
    out = evaluate_significance({'exposures': 1000, 'conversions': 100},
                                  {'exposures': 1000, 'conversions': 50})
    assert out['significant'] is True
    assert out['direction'] == 'negative'


def test_significance_respects_alpha_threshold():
    control = {'exposures': 1000, 'conversions': 50}
    treatment = {'exposures': 1000, 'conversions': 68}
    strict = evaluate_significance(control, treatment, alpha=0.05)
    loose = evaluate_significance(control, treatment, alpha=0.10)
    assert strict['significant'] is False
    assert loose['significant'] is True


def test_significance_zero_control_rate_reports_infinite_lift():
    out = evaluate_significance({'exposures': 1000, 'conversions': 0},
                                  {'exposures': 1000, 'conversions': 10})
    assert out['significant'] is True
    assert out['direction'] == 'positive'
    assert out['lift'] == float('inf')


def test_finalize_promote_winner():
    out = finalize_experiment(
        experiment_id='e1',
        control={'exposures': 1000, 'conversions': 50},
        treatment={'exposures': 1000, 'conversions': 100},
        runtime_days=7,
    )
    assert out['decision'] == 'promote_winner'


def test_finalize_rollback_loser():
    out = finalize_experiment(
        experiment_id='e1',
        control={'exposures': 1000, 'conversions': 100},
        treatment={'exposures': 1000, 'conversions': 50},
        runtime_days=7,
    )
    assert out['decision'] == 'rollback_to_control'


def test_finalize_continues_when_sample_low():
    out = finalize_experiment(
        experiment_id='e1',
        control={'exposures': 50, 'conversions': 5},
        treatment={'exposures': 50, 'conversions': 10},
        runtime_days=2,
    )
    assert out['decision'] == 'continue'
    assert out['sample_ok'] is False


def test_finalize_inconclusive_after_max_runtime():
    out = finalize_experiment(
        experiment_id='e1',
        control={'exposures': 50, 'conversions': 5},
        treatment={'exposures': 50, 'conversions': 6},
        runtime_days=30,
        max_runtime_days=28,
    )
    assert out['decision'] == 'inconclusive_fallback_control'


def test_finalize_continue_when_significant_but_low_sample():
    # significant by z but sample too low → continue to gather more
    out = finalize_experiment(
        experiment_id='e1',
        control={'exposures': 50, 'conversions': 5},
        treatment={'exposures': 50, 'conversions': 25},
        runtime_days=3,
        min_sample_per_arm=200,
    )
    assert out['decision'] == 'continue'


def test_archive_writes_jsonl(tmp_path):
    p = str(tmp_path / 'arch.jsonl')
    out = finalize_experiment(experiment_id='e1',
                                control={'exposures': 1000, 'conversions': 50},
                                treatment={'exposures': 1000, 'conversions': 100},
                                runtime_days=10)
    assert archive_experiment('e1', out, p) is True
    assert open(p).read().strip() != ''


def test_archive_no_path_returns_false():
    assert archive_experiment('e1', {}, '') is False
