"""S55 — bandit tests."""
from __future__ import annotations

import random

from src.services.cro_action_bandit import (
    best_action, init_state, select, stats_for, update,
)


def test_init_state_empty():
    s = init_state()
    assert s['arms'] == {}


def test_update_increments_n_and_mean():
    s = init_state()
    update(s, 'Kitchen', 'price_drop', 1.0)
    update(s, 'Kitchen', 'price_drop', 3.0)
    arm = s['arms']['Kitchen|price_drop']
    assert arm['n'] == 2
    assert arm['mean'] == 2.0


def test_best_action_prefers_untried():
    s = init_state()
    update(s, 'K', 'price_drop', 5.0)
    # promote/image_refresh/threshold_change 未试过 → 优先选 promote (第一个)
    assert best_action(s, 'K') == 'promote'


def test_best_action_picks_highest_mean_when_all_tried():
    s = init_state()
    for a, r in [('price_drop', 0.1), ('promote', 1.5),
                 ('image_refresh', 0.5), ('threshold_change', 0.3)]:
        update(s, 'K', a, r)
    assert best_action(s, 'K') == 'promote'


def test_select_explore_branch_with_low_random():
    rng = random.Random(0)
    # rng.random() 第一次会出 0.84... → 不算 explore (epsilon=0.15)
    # 强制 epsilon=1.0 → 必 explore
    s = init_state()
    out = select(s, 'K', epsilon=1.0, rng=rng)
    assert out['mode'] == 'explore'
    assert out['action'] in ('price_drop', 'promote',
                             'image_refresh', 'threshold_change')


def test_select_exploit_branch_with_zero_epsilon():
    s = init_state()
    update(s, 'K', 'price_drop', 5.0)
    update(s, 'K', 'promote', 1.0)
    update(s, 'K', 'image_refresh', 1.0)
    update(s, 'K', 'threshold_change', 1.0)
    out = select(s, 'K', epsilon=0.0)
    assert out['mode'] == 'exploit'
    assert out['action'] == 'price_drop'


def test_categories_are_isolated():
    s = init_state()
    update(s, 'Kitchen', 'price_drop', 5.0)
    update(s, 'Garden', 'promote', 5.0)
    assert 'Kitchen|price_drop' in s['arms']
    assert 'Garden|promote' in s['arms']
    assert 'Kitchen|promote' not in s['arms']


def test_stats_for_returns_all_actions():
    s = init_state()
    update(s, 'K', 'promote', 2.0)
    out = stats_for(s, 'K')
    assert len(out) == 4
    promote = next(x for x in out if x['action'] == 'promote')
    assert promote['n'] == 1
    assert promote['mean_reward'] == 2.0


def test_select_returns_valid_action_when_state_empty():
    out = select(init_state(), 'K', epsilon=0.0)
    assert out['action'] in ('price_drop', 'promote',
                             'image_refresh', 'threshold_change')
    assert out['mode'] == 'exploit'
