"""S65 — UCB1 bandit tests."""
from __future__ import annotations

import math

from src.services.cro_action_bandit_ucb import (
    ACTIONS, best_action, init_state, select, stats_for, ucb_score, update,
)


def test_init_state_empty():
    s = init_state()
    assert s == {'arms': {}}


def test_select_explores_untried_arms_first():
    s = init_state()
    pick = select(s, 'Kitchen')
    assert pick['mode'] == 'ucb_explore'
    assert pick['action'] == ACTIONS[0]


def test_update_increments_n_and_sum():
    s = init_state()
    update(s, 'Kitchen', 'promote', 0.5)
    update(s, 'Kitchen', 'promote', 1.5)
    arm = s['arms']['Kitchen|promote']
    assert arm['n'] == 2
    assert arm['sum'] == 2.0


def test_best_action_picks_highest_mean():
    s = init_state()
    update(s, 'K', 'promote', 1.0)
    update(s, 'K', 'price_drop', 0.1)
    assert best_action(s, 'K') == 'promote'


def test_best_action_none_when_empty():
    assert best_action(init_state(), 'K') is None


def test_ucb_score_zero_arm_is_infinity():
    assert ucb_score(0.0, 0, 100) == float('inf')


def test_ucb_score_balances_mean_and_uncertainty():
    # high mean low n vs low mean high n: should still favor exploration
    high_mean_low_n = ucb_score(0.6, 1, 100)
    low_mean_high_n = ucb_score(0.5, 50, 100)
    assert high_mean_low_n > low_mean_high_n


def test_select_after_all_arms_played(tmp_path):
    s = init_state()
    # play each arm once with same reward
    for a in ACTIONS:
        update(s, 'K', a, 1.0)
    # play one extra time on 'promote' to reduce its uncertainty
    update(s, 'K', 'promote', 1.0)
    pick = select(s, 'K')
    assert pick['mode'] == 'ucb'
    # less-explored arm with same mean should be picked due to UCB bonus
    assert pick['action'] != 'promote'


def test_select_picks_higher_mean_when_n_equal():
    s = init_state()
    for a in ACTIONS:
        update(s, 'K', a, 0.1)
    update(s, 'K', 'promote', 0.9)  # boost promote mean
    update(s, 'K', 'price_drop', 0.1)
    update(s, 'K', 'image_refresh', 0.1)
    update(s, 'K', 'threshold_change', 0.1)
    pick = select(s, 'K')
    assert pick['action'] == 'promote'


def test_stats_for_returns_all_actions():
    s = init_state()
    update(s, 'K', 'promote', 1.0)
    rows = stats_for(s, 'K')
    assert len(rows) == len(ACTIONS)
    promote = next(r for r in rows if r['action'] == 'promote')
    assert promote['n'] == 1
    assert promote['mean'] == 1.0
    assert promote['ucb'] is not None
    untried = next(r for r in rows if r['action'] == 'price_drop')
    assert untried['n'] == 0
    assert untried['ucb'] is None


def test_ucb_score_total_zero_returns_mean():
    assert ucb_score(0.7, 1, 0) == 0.7


def test_select_deterministic_tiebreak():
    s = init_state()
    for a in ACTIONS:
        update(s, 'K', a, 1.0)
    p1 = select(s, 'K')
    p2 = select(s, 'K')
    assert p1['action'] == p2['action']
