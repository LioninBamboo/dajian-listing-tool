"""S60 — dry-run drill tests."""
from __future__ import annotations

import random

from src.services.cro_action_bandit import init_state
from src.services.cro_dryrun_drill import (
    render_drill_report, simulate_day, simulate_drill,
)


def test_simulate_day_records_each_category():
    state = init_state()
    log = simulate_day(state, ['K', 'G'],
                       reward_simulator=lambda c, a: 0.1,
                       epsilon=0.0, rng=random.Random(0))
    assert {d['category'] for d in log} == {'K', 'G'}


def test_simulate_drill_default_converges_to_promote():
    # 默认 reward simulator 中 promote mean=0.30 显著最高
    result = simulate_drill(days=60, seed=7)
    # 每个品类最终 best action 应是 promote (高概率)
    promote_wins = sum(1 for v in result['final_best_action'].values()
                       if v == 'promote')
    assert promote_wins >= 2  # 3 品类至少 2 个收敛到 promote


def test_simulate_drill_total_reward_positive():
    result = simulate_drill(days=30, seed=42)
    assert result['total_reward'] > 0


def test_simulate_drill_uses_custom_reward():
    # 让 image_refresh 始终最高
    rs = lambda cat, action: 1.0 if action == 'image_refresh' else 0.0
    result = simulate_drill(days=20, categories=['X'], reward_simulator=rs)
    assert result['final_best_action']['X'] == 'image_refresh'


def test_simulate_drill_outputs_traffic_light():
    result = simulate_drill(days=10)
    assert 'status_overall' in result['traffic_light']
    assert result['traffic_light']['status_overall'] in (
        'green', 'yellow', 'red')


def test_simulate_drill_seed_reproducible():
    a = simulate_drill(days=10, seed=99)
    b = simulate_drill(days=10, seed=99)
    assert a['total_reward'] == b['total_reward']
    assert a['final_best_action'] == b['final_best_action']


def test_render_drill_report_smoke():
    r = simulate_drill(days=5, seed=1)
    text = render_drill_report(r)
    assert 'dry-run' in text
    assert 'Kitchen' in text
    assert 'traffic-light' in text
