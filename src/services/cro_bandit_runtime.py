"""S57 — Bandit 接入主流程.

把 S55 cro_action_bandit 持久化到 logs/cro_bandit_state.json,
提供 cro_daily_runner 可直接调用的 select_action_for / record_outcome.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.services.cro_action_bandit import (
    DEFAULT_EPSILON, init_state, select, update,
)

DEFAULT_STATE_PATH = Path('logs/cro_bandit_state.json')


def load_state(path: Path = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    if not path.exists():
        return init_state()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or 'arms' not in data:
            return init_state()
        return data
    except (json.JSONDecodeError, OSError):
        return init_state()


def save_state(state: Dict[str, Any],
               path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2),
        encoding='utf-8',
    )


def select_action_for(category: str,
                      candidate_actions: Tuple[str, ...],
                      epsilon: float = DEFAULT_EPSILON,
                      path: Path = DEFAULT_STATE_PATH,
                      ) -> Dict[str, Any]:
    state = load_state(path)
    out = select(state, category, actions=candidate_actions, epsilon=epsilon)
    out['category'] = category
    return out


def record_outcome(category: str, action: str, reward: float,
                   path: Path = DEFAULT_STATE_PATH,
                   ) -> Dict[str, Any]:
    state = load_state(path)
    update(state, category, action, reward)
    save_state(state, path)
    return state


def reward_from_lift(lift_pct: Optional[float],
                     roi: Optional[float] = None,
                     fallback: float = 0.0) -> float:
    """统一 reward 计算: 默认 lift, 但若 ROI < 0.5 视为重度负反馈."""
    if lift_pct is None and roi is None:
        return fallback
    base = lift_pct if lift_pct is not None else 0.0
    if roi is not None and roi < 0.5:
        return base - 0.5
    return base
