"""S65 — UCB1 替代 epsilon-greedy bandit.

接口与 cro_action_bandit 兼容: init_state / update / best_action / select /
stats_for, 但 select 用 UCB1 公式:
  ucb_i = mean_i + sqrt(2 * ln(T) / n_i)
未拉过的 arm 优先级无穷大, 自动先各拉一次.

cro_bandit_runtime 可以通过 STRATEGY=ucb 环境变量切换到本模块.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

ACTIONS = ('price_drop', 'promote', 'image_refresh', 'threshold_change')
EXPLORATION_C = 2.0  # UCB1 经典常数


def init_state() -> Dict[str, Any]:
    return {'arms': {}}


def _key(category: str, action: str) -> str:
    return f'{category}|{action}'


def update(state: Dict[str, Any], category: str, action: str,
           reward: float) -> Dict[str, Any]:
    arms = state.setdefault('arms', {})
    k = _key(category, action)
    arm = arms.setdefault(k, {'n': 0, 'sum': 0.0})
    arm['n'] += 1
    arm['sum'] += float(reward)
    return state


def _arm_stats(state: Dict[str, Any], category: str,
               action: str) -> Tuple[int, float]:
    arm = (state.get('arms') or {}).get(_key(category, action))
    if not arm or arm['n'] == 0:
        return 0, 0.0
    return arm['n'], arm['sum'] / arm['n']


def _category_total(state: Dict[str, Any], category: str,
                    actions: tuple) -> int:
    return sum(_arm_stats(state, category, a)[0] for a in actions)


def best_action(state: Dict[str, Any], category: str,
                actions: tuple = ACTIONS) -> Optional[str]:
    """按平均 reward 选最高 (跟 epsilon 版语义一致)."""
    scored = [(a, _arm_stats(state, category, a)) for a in actions]
    scored = [(a, n, mean) for a, (n, mean) in scored if n > 0]
    if not scored:
        return None
    return max(scored, key=lambda r: (r[2], r[1], r[0]))[0]


def ucb_score(mean: float, n_arm: int, total_n: int,
              c: float = EXPLORATION_C) -> float:
    if n_arm == 0:
        return float('inf')
    if total_n <= 0:
        return mean
    return mean + math.sqrt(c * math.log(total_n) / n_arm)


def select(state: Dict[str, Any], category: str,
           actions: tuple = ACTIONS,
           c: float = EXPLORATION_C) -> Dict[str, Any]:
    total = _category_total(state, category, actions)
    untried = [a for a in actions
               if _arm_stats(state, category, a)[0] == 0]
    if untried:
        chosen = untried[0]  # 确定顺序便于测试可重现
        return {'action': chosen, 'mode': 'ucb_explore',
                'score': float('inf'), 'total_n': total}
    scored = []
    for a in actions:
        n, mean = _arm_stats(state, category, a)
        scored.append((a, ucb_score(mean, n, total, c), mean, n))
    scored.sort(key=lambda r: (-r[1], -r[2], r[0]))
    chosen, score, _, _ = scored[0]
    return {'action': chosen, 'mode': 'ucb', 'score': round(score, 6),
            'total_n': total}


def stats_for(state: Dict[str, Any], category: str,
              actions: tuple = ACTIONS) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    total = _category_total(state, category, actions)
    for a in actions:
        n, mean = _arm_stats(state, category, a)
        out.append({
            'action': a,
            'n': n,
            'mean': round(mean, 4),
            'ucb': (round(ucb_score(mean, n, total), 4)
                    if n > 0 else None),
        })
    return out
