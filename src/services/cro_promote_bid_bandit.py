"""S71 — promote 出价档位 bandit.

候选档位 BID_LEVELS = (5,10,15,20) %; 复用 cro_action_bandit 接口 (k arm).
state 单独存 logs/cro_promote_bid_state.json, 不污染 action bandit.
若 cro_pricing_feedback 显示某 category inelastic, 自动屏蔽高档位 (>=15).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.services.cro_action_bandit import (
    best_action, init_state, select, update,
)

BID_LEVELS: Tuple[str, ...] = ('bid_5', 'bid_10', 'bid_15', 'bid_20')
LEVEL_TO_PCT = {'bid_5': 5, 'bid_10': 10, 'bid_15': 15, 'bid_20': 20}
DEFAULT_STATE_PATH = Path('logs/cro_promote_bid_state.json')
INELASTIC_MAX_PCT = 10  # inelastic 时不出 >10%


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return init_state()
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return init_state()


def _save(state: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True),
                    encoding='utf-8')


def _allowed_levels(elasticity: Optional[str]) -> Tuple[str, ...]:
    if elasticity == 'inelastic':
        return tuple(l for l in BID_LEVELS
                     if LEVEL_TO_PCT[l] <= INELASTIC_MAX_PCT)
    return BID_LEVELS


def select_bid(category: str,
               elasticity: Optional[str] = None,
               *,
               epsilon: float = 0.15,
               state_path: Path = DEFAULT_STATE_PATH,
               ) -> Dict[str, Any]:
    state = _load(state_path)
    allowed = _allowed_levels(elasticity)
    out = select(state, category, actions=allowed, epsilon=epsilon)
    out['bid_pct'] = LEVEL_TO_PCT[out['action']]
    out['allowed_levels'] = list(allowed)
    out['category'] = category
    return out


def record_bid_outcome(category: str, level: str, reward: float,
                       state_path: Path = DEFAULT_STATE_PATH,
                       ) -> Dict[str, Any]:
    if level not in BID_LEVELS:
        raise ValueError(f'unknown bid level: {level}')
    state = _load(state_path)
    update(state, category, level, reward)
    _save(state, state_path)
    return state


def best_bid_for(category: str,
                 state_path: Path = DEFAULT_STATE_PATH,
                 ) -> Optional[Dict[str, Any]]:
    state = _load(state_path)
    arms = state.get('arms') or {}
    total_n = sum(arms.get(f'{category}|{l}', {}).get('n', 0)
                  for l in BID_LEVELS)
    if total_n == 0:
        return None
    chosen = best_action(state, category, actions=BID_LEVELS)
    if not chosen:
        return None
    return {'level': chosen, 'bid_pct': LEVEL_TO_PCT[chosen]}


def stats_for_category(category: str,
                       state_path: Path = DEFAULT_STATE_PATH,
                       ) -> List[Dict[str, Any]]:
    state = _load(state_path)
    arms = state.get('arms') or {}
    rows = []
    for lvl in BID_LEVELS:
        arm = arms.get(f'{category}|{lvl}', {'n': 0, 'sum': 0.0,
                                             'mean': 0.0})
        n = arm.get('n', 0)
        rows.append({
            'level': lvl,
            'bid_pct': LEVEL_TO_PCT[lvl],
            'n': n,
            'mean': round(arm.get('mean', 0.0), 4),
        })
    return rows
