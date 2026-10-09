"""S32 \u2014 \u9608\u503c\u53cd\u54fa\u2192pending."""
from __future__ import annotations

from pathlib import Path

from src.services.cro_thresholds import (
    apply_suggestions_to_pending, ensure_schema, load_pending_thresholds,
    _write_thresholds,
)


def test_apply_suggestions_writes_pending(tmp_path: Path):
    db = tmp_path / 'e.db'
    ensure_schema(db)
    # \u5148\u5199\u5165\u5f53\u524d\u751f\u4ea7\u9608\u503c, \u4ee5\u4f9b\u5408\u5e76
    _write_thresholds(db, 'cro_thresholds', {
        'CAT_X': {'ctr': 0.020, 'cvr': 0.030, 'str': 0.0008, 'samples': 50},
    })
    suggestions = [
        {'category_id': 'CAT_X', 'action': 'price_drop', 'metric': 'ctr',
         'current': 0.020, 'suggested': 0.018, 'direction': 'relax',
         'improved_rate': 0.20, 'samples': 12},
    ]
    n = apply_suggestions_to_pending(suggestions, db_path=db)
    assert n == 1
    pending = load_pending_thresholds(db)
    assert 'CAT_X' in pending
    assert pending['CAT_X']['ctr'] == 0.018
    # cvr/str \u5e94\u4ece\u751f\u4ea7\u9608\u503c\u7ee7\u627f
    assert pending['CAT_X']['cvr'] == 0.030


def test_apply_suggestions_empty_returns_zero(tmp_path: Path):
    db = tmp_path / 'e.db'
    n = apply_suggestions_to_pending([], db_path=db)
    assert n == 0


def test_apply_suggestions_merges_multiple_metrics_for_same_category(tmp_path: Path):
    db = tmp_path / 'e.db'
    ensure_schema(db)
    suggestions = [
        {'category_id': 'CAT_Y', 'action': 'price_drop', 'metric': 'ctr',
         'current': 0.015, 'suggested': 0.014, 'direction': 'relax',
         'improved_rate': 0.20, 'samples': 8},
        {'category_id': 'CAT_Y', 'action': 'fill_specifics', 'metric': 'cvr',
         'current': 0.020, 'suggested': 0.022, 'direction': 'tighten',
         'improved_rate': 0.70, 'samples': 10},
    ]
    n = apply_suggestions_to_pending(suggestions, db_path=db)
    assert n == 1  # \u540c\u4e00 category \u5408\u5e76
    pending = load_pending_thresholds(db)
    assert pending['CAT_Y']['ctr'] == 0.014
    assert pending['CAT_Y']['cvr'] == 0.022


def test_feedback_run_dry_run_does_not_write(tmp_path: Path, monkeypatch):
    from scripts import cro_threshold_feedback as mod
    fake_audit = {
        'total_evaluated': 5,
        'rows': [
            {'category': 'CAT_Z', 'action': 'price_drop', 'verdict': 'flat'}
            for _ in range(8)
        ],
    }
    monkeypatch.setattr('scripts.cro_effect_audit.evaluate_actions',
                        lambda **k: fake_audit)
    monkeypatch.setattr(mod, 'load_thresholds',
                        lambda: {'CAT_Z': {'ctr': 0.02, 'cvr': 0.03}})
    rep = mod.run(write=False)
    assert rep['evaluated'] == 5
    assert rep['pending_written'] == 0
