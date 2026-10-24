"""S57 — bandit runtime tests."""
from __future__ import annotations

from src.services.cro_bandit_runtime import (
    load_state, record_outcome, reward_from_lift, save_state,
    select_action_for,
)


def test_load_state_missing_returns_empty(tmp_path):
    s = load_state(tmp_path / 'nope.json')
    assert s == {'arms': {}}


def test_load_state_corrupt_returns_empty(tmp_path):
    p = tmp_path / 'bad.json'
    p.write_text('NOT_JSON', encoding='utf-8')
    assert load_state(p) == {'arms': {}}


def test_save_load_round_trip(tmp_path):
    p = tmp_path / 's.json'
    save_state({'arms': {'K|promote': {'n': 3, 'mean': 0.5}}}, p)
    s2 = load_state(p)
    assert s2['arms']['K|promote']['mean'] == 0.5


def test_select_action_for_returns_category(tmp_path):
    p = tmp_path / 's.json'
    out = select_action_for('Kitchen', ('promote', 'price_drop'),
                            epsilon=0.0, path=p)
    assert out['category'] == 'Kitchen'
    assert out['action'] in ('promote', 'price_drop')


def test_record_outcome_persists(tmp_path):
    p = tmp_path / 's.json'
    record_outcome('Kitchen', 'promote', 0.20, path=p)
    record_outcome('Kitchen', 'promote', 0.40, path=p)
    state = load_state(p)
    arm = state['arms']['Kitchen|promote']
    assert arm['n'] == 2
    assert abs(arm['mean'] - 0.30) < 1e-9


def test_reward_from_lift_uses_lift_when_roi_ok():
    assert reward_from_lift(0.25, roi=1.5) == 0.25


def test_reward_from_lift_penalizes_low_roi():
    # ROI 0.3 → 强负反馈
    assert reward_from_lift(0.10, roi=0.3) == -0.4


def test_reward_from_lift_fallback_when_both_none():
    assert reward_from_lift(None, None, fallback=0.0) == 0.0
