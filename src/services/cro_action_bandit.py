"""S55 — Epsilon-greedy bandit 选 action.

按 (category, action) 维度记录历史 lift / ROI, 用 epsilon-greedy 在
{price_drop, promote, image_refresh, threshold_change} 之间选最优。

无 sklearn / torch 依赖。状态可序列化为 dict (调用方负责持久化)。
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

ACTIONS = ('price_drop', 'promote', 'image_refresh', 'threshold_change')
DEFAULT_EPSILON = 0.15


def init_state() -> Dict[str, Any]:
    return {'arms': {}}  # arms[(cat, action)] = {n, mean}


def _key(category: str, action: str) -> str:
    return f'{category}|{action}'


def update(state: Dict[str, Any], category: str, action: str,
           reward: float) -> None:
    arms = state.setdefault('arms', {})
    k = _key(category, action)
    arm = arms.get(k) or {'n': 0, 'mean': 0.0}
    n_new = arm['n'] + 1
    # 增量均值
    mean_new = arm['mean'] + (reward - arm['mean']) / n_new
    arms[k] = {'n': n_new, 'mean': mean_new}


def best_action(state: Dict[str, Any], category: str,
                actions: tuple = ACTIONS) -> Optional[str]:
    arms = state.get('arms', {})
    seen = [(a, arms.get(_key(category, a)))
            for a in actions]
    # 优先选未试过的 (n=0) arm 之一
    untried = [a for a, arm in seen if not arm or arm['n'] == 0]
    if untried:
        return untried[0]
    return max(seen, key=lambda x: x[1]['mean'])[0]


def select(state: Dict[str, Any], category: str,
           actions: tuple = ACTIONS,
           epsilon: float = DEFAULT_EPSILON,
           rng: Optional[random.Random] = None,
           ) -> Dict[str, Any]:
    """返回 {'action': str, 'mode': 'explore'|'exploit'}."""
    rng = rng or random.Random()
    if rng.random() < epsilon:
        chosen = rng.choice(list(actions))
        return {'action': chosen, 'mode': 'explore'}
    return {'action': best_action(state, category, actions),
            'mode': 'exploit'}


def stats_for(state: Dict[str, Any], category: str,
              actions: tuple = ACTIONS) -> List[Dict[str, Any]]:
    arms = state.get('arms', {})
    out = []
    for a in actions:
        arm = arms.get(_key(category, a)) or {'n': 0, 'mean': 0.0}
        out.append({'action': a, 'n': arm['n'],
                    'mean_reward': round(arm['mean'], 4)})
    return out
