"""S61 — daily_runner bandit shadow tests."""
from __future__ import annotations

from src.services.cro_action_bandit import ACTIONS
from src.services.cro_bandit_runtime import record_outcome
from src.services.cro_daily_runner_bandit import (
    category_is_trusted, decide_action, disagreement_rate, shadow_compare,
)


def _seed_outcomes(state_path, n=10, action='promote', cat='Kitchen'):
    for _ in range(n):
        record_outcome(cat, action, 0.5, path=state_path)


def _seed_all_arms_with_winner(state_path, winner='promote',
                               cat='Kitchen', n=10):
    """让所有 arm 都被试过, winner 平均 reward 最高."""
    for a in ACTIONS:
        if a == winner:
            for _ in range(n):
                record_outcome(cat, a, 1.0, path=state_path)
        else:
            for _ in range(n):
                record_outcome(cat, a, 0.0, path=state_path)


def test_category_not_trusted_when_no_history(tmp_path):
    sp = tmp_path / 's.json'
    assert not category_is_trusted('Kitchen', path=sp)


def test_category_trusted_after_min_outcomes(tmp_path):
    sp = tmp_path / 's.json'
    _seed_outcomes(sp, n=10)
    assert category_is_trusted('Kitchen', path=sp)


def test_shadow_compare_records_agreement(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    _seed_all_arms_with_winner(sp, winner='promote')
    rec = shadow_compare('promote', 'Kitchen',
                         state_path=sp, shadow_log=log)
    assert rec['agree']
    assert rec['trusted']
    assert log.exists()


def test_shadow_compare_records_disagreement(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    _seed_all_arms_with_winner(sp, winner='promote')
    rec = shadow_compare('price_drop', 'Kitchen',
                         state_path=sp, shadow_log=log)
    assert not rec['agree']
    assert rec['bandit_action'] == 'promote'


def test_decide_action_shadow_keeps_rule(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    _seed_outcomes(sp, n=10, action='promote')
    final, meta = decide_action('price_drop', 'Kitchen', mode='shadow',
                                state_path=sp, shadow_log=log)
    assert final == 'price_drop'
    assert meta['final_source'] == 'rule'


def test_decide_action_active_uses_bandit_when_trusted(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    _seed_all_arms_with_winner(sp, winner='promote')
    final, meta = decide_action('price_drop', 'Kitchen', mode='active',
                                state_path=sp, shadow_log=log, epsilon=0.0)
    assert final == 'promote'
    assert meta['final_source'] == 'bandit'


def test_decide_action_active_falls_back_when_untrusted(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    _seed_outcomes(sp, n=3, action='promote')  # < 10 → 不可信
    final, meta = decide_action('price_drop', 'Kitchen', mode='active',
                                state_path=sp, shadow_log=log)
    assert final == 'price_drop'
    assert meta['final_source'] == 'rule'


def test_disagreement_rate_only_counts_trusted(tmp_path):
    sp = tmp_path / 's.json'
    log = tmp_path / 'shadow.jsonl'
    _seed_all_arms_with_winner(sp, winner='promote')
    # bandit picks 'promote' deterministically
    shadow_compare('promote', 'Kitchen', state_path=sp, shadow_log=log)
    shadow_compare('price_drop', 'Kitchen', state_path=sp, shadow_log=log)
    shadow_compare('image_refresh', 'Kitchen', state_path=sp, shadow_log=log)
    out = disagreement_rate(log, category='Kitchen')
    assert out['total'] == 3
    assert out['disagree'] == 2
    assert out['rate'] == round(2 / 3, 4)


def test_disagreement_rate_missing_log(tmp_path):
    out = disagreement_rate(tmp_path / 'nope.jsonl')
    assert out == {'total': 0, 'disagree': 0, 'rate': 0.0}
