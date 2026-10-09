"""S61 — cro_daily_runner 接 bandit (shadow 模式).

不替换现有规则建议; 仅在 bandit 已积累 ≥MIN_OUTCOMES_TO_TRUST 时, 影子地
对比 bandit 的选择与规则的选择, 记录 disagreement 比例。
未来当 disagreement 收敛后再切到 active 模式 (bandit 真覆盖规则)。

mode='shadow' (默认): 不改变 action, 只记录
mode='active': bandit 选择真覆盖规则建议
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.services.cro_action_bandit import ACTIONS
from src.services.cro_bandit_runtime import (
    DEFAULT_STATE_PATH, load_state, select_action_for,
)

MIN_OUTCOMES_TO_TRUST = 10
SHADOW_LOG_DEFAULT = Path('logs/cro_bandit_shadow.jsonl')


def _category_total_outcomes(state: Dict[str, Any], category: str) -> int:
    arms = state.get('arms') or {}
    return sum(arms.get(f'{category}|{a}', {}).get('n', 0) for a in ACTIONS)


def category_is_trusted(category: str,
                        state: Optional[Dict[str, Any]] = None,
                        path: Path = DEFAULT_STATE_PATH,
                        min_outcomes: int = MIN_OUTCOMES_TO_TRUST,
                        ) -> bool:
    state = state if state is not None else load_state(path)
    return _category_total_outcomes(state, category) >= min_outcomes


def shadow_compare(rule_action: str,
                   category: str,
                   *,
                   state_path: Path = DEFAULT_STATE_PATH,
                   shadow_log: Path = SHADOW_LOG_DEFAULT,
                   min_outcomes: int = MIN_OUTCOMES_TO_TRUST,
                   epsilon: float = 0.0,  # shadow 时 0 探索, 看真 best
                   ) -> Dict[str, Any]:
    state = load_state(state_path)
    trusted = _category_total_outcomes(state, category) >= min_outcomes
    bandit_pick = select_action_for(category, ACTIONS,
                                    epsilon=epsilon, path=state_path)
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'category': category,
        'rule_action': rule_action,
        'bandit_action': bandit_pick['action'],
        'agree': rule_action == bandit_pick['action'],
        'trusted': trusted,
    }
    _append_jsonl(shadow_log, record)
    return record


def decide_action(rule_action: str,
                  category: str,
                  mode: str = 'shadow',
                  *,
                  state_path: Path = DEFAULT_STATE_PATH,
                  shadow_log: Path = SHADOW_LOG_DEFAULT,
                  min_outcomes: int = MIN_OUTCOMES_TO_TRUST,
                  epsilon: float = 0.15,
                  ) -> Tuple[str, Dict[str, Any]]:
    """主入口. 返回 (final_action, decision_meta)."""
    cmp_record = shadow_compare(
        rule_action, category,
        state_path=state_path, shadow_log=shadow_log,
        min_outcomes=min_outcomes, epsilon=0.0,
    )
    if mode == 'active' and cmp_record['trusted']:
        bandit_pick = select_action_for(
            category, ACTIONS, epsilon=epsilon, path=state_path)
        return bandit_pick['action'], {
            **cmp_record, 'final_source': 'bandit',
            'mode': bandit_pick['mode'],
        }
    return rule_action, {**cmp_record, 'final_source': 'rule'}


def disagreement_rate(shadow_log: Path = SHADOW_LOG_DEFAULT,
                      category: Optional[str] = None,
                      ) -> Dict[str, Any]:
    if not shadow_log.exists():
        return {'total': 0, 'disagree': 0, 'rate': 0.0}
    total = 0
    disagree = 0
    for line in shadow_log.read_text(encoding='utf-8').splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if category and r.get('category') != category:
            continue
        if not r.get('trusted'):
            continue
        total += 1
        if not r.get('agree'):
            disagree += 1
    rate = (disagree / total) if total else 0.0
    return {'total': total, 'disagree': disagree, 'rate': round(rate, 4)}


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
