"""S60 — 端到端 CRO 闭环 dry-run 演练.

模拟 N 天 (默认 30) 流程: 每日入队 → bandit 选 action → mock outcome → 更新 bandit
→ 周末跑周报 → 月末出 traffic-light. 不调真 eBay/Marketing API.

作为 release gate: 验证整个流水线指标演变是否合理 (lift > 0, bandit 收敛)。
"""
from __future__ import annotations

import random
from typing import Any, Callable, Dict, List, Optional

from src.services.cro_action_bandit import (
    ACTIONS, init_state, select, update,
)
from src.services.cro_dashboard_traffic_light import build_dashboard


def simulate_day(state: Dict[str, Any], categories: List[str],
                 reward_simulator: Callable[[str, str], float],
                 epsilon: float = 0.15,
                 rng: Optional[random.Random] = None,
                 ) -> List[Dict[str, Any]]:
    rng = rng or random.Random()
    day_log: List[Dict[str, Any]] = []
    for cat in categories:
        decision = select(state, cat, actions=ACTIONS,
                          epsilon=epsilon, rng=rng)
        reward = reward_simulator(cat, decision['action'])
        update(state, cat, decision['action'], reward)
        day_log.append({
            'category': cat,
            'action': decision['action'],
            'mode': decision['mode'],
            'reward': reward,
        })
    return day_log


def simulate_drill(days: int = 30,
                   categories: Optional[List[str]] = None,
                   reward_simulator: Optional[Callable[[str, str], float]] = None,
                   seed: int = 42,
                   ) -> Dict[str, Any]:
    """跑 N 天闭环, 输出 bandit 收敛信息 + traffic-light 终态."""
    categories = categories or ['Kitchen', 'Garden', 'Apparel']
    rng = random.Random(seed)

    if reward_simulator is None:
        # 默认: promote 真有效 (mean=0.3), 其他平均 0
        true_means = {
            'promote': 0.30, 'price_drop': 0.05,
            'image_refresh': 0.10, 'threshold_change': 0.0,
        }
        def reward_simulator(cat, action):
            return rng.gauss(true_means.get(action, 0.0), 0.10)

    state = init_state()
    history: List[List[Dict[str, Any]]] = []
    for _ in range(days):
        history.append(simulate_day(state, categories,
                                    reward_simulator,
                                    epsilon=0.15, rng=rng))

    # 计算每个品类最终选了哪个 action 最多
    final_choice: Dict[str, str] = {}
    for cat in categories:
        best_arm = None
        best_mean = -1e9
        for action in ACTIONS:
            arm = state['arms'].get(f'{cat}|{action}')
            if arm and arm['mean'] > best_mean:
                best_mean = arm['mean']
                best_arm = action
        final_choice[cat] = best_arm or 'unknown'

    # 模拟终态 traffic-light
    tl = build_dashboard(
        approvals_fetcher=lambda: {'total': len(categories)},
        alerts_fetcher=lambda: {'high_priority_count': 0},
        returns_fetcher=lambda: {'high_return_count': 0},
        inventory_fetcher=lambda: {'throttle_skus': []},
    )

    total_reward = sum(d['reward'] for day in history for d in day)
    return {
        'days': days,
        'categories': categories,
        'final_best_action': final_choice,
        'total_reward': round(total_reward, 4),
        'avg_reward_per_decision': round(
            total_reward / max(1, days * len(categories)), 4),
        'traffic_light': tl,
        'bandit_state': state,
    }


def render_drill_report(result: Dict[str, Any]) -> str:
    lines = [
        f'🎯 CRO 端到端 dry-run ({result["days"]} 天)',
        f'累计 reward: {result["total_reward"]} '
        f'(avg/decision {result["avg_reward_per_decision"]})',
        '',
        '各品类收敛到的最优 action:',
    ]
    for cat, act in result['final_best_action'].items():
        lines.append(f'  • {cat}: {act}')
    lines.append('')
    tl = result['traffic_light']
    lines.append(f'终态 traffic-light: {tl["status_overall"].upper()}')
    return '\n'.join(lines)
