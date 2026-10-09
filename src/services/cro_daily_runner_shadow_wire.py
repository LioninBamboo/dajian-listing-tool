"""S73 — daily_runner shadow wiring helper.

不修改 cro_daily_runner.py; 提供一个可在 P1 入队后调用的 shadow_log_actions
迭代每条 action, 取 category 与 action 字段, 调 cro_daily_runner_bandit.shadow_compare.
失败/缺字段直接跳过, 永不影响 enqueue 主流程.

未来 daily_runner 可在 enqueue 后加一行:
    shadow_log_actions(p1_actions)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.services.cro_daily_runner_bandit import (
    SHADOW_LOG_DEFAULT, shadow_compare,
)
from src.services.cro_bandit_runtime import DEFAULT_STATE_PATH


def _category_of(action: Dict[str, Any]) -> Optional[str]:
    cat = (action.get('category')
           or action.get('product_category')
           or (action.get('product') or {}).get('category'))
    if isinstance(cat, str) and cat.strip():
        return cat.strip()
    return None


def shadow_log_actions(actions: Iterable[Dict[str, Any]],
                       *,
                       state_path: Path = DEFAULT_STATE_PATH,
                       shadow_log: Path = SHADOW_LOG_DEFAULT,
                       ) -> Dict[str, Any]:
    logged: List[Dict[str, Any]] = []
    skipped = 0
    errors = 0
    for action in actions:
        rule_action = action.get('action')
        category = _category_of(action)
        if not rule_action or not category:
            skipped += 1
            continue
        try:
            rec = shadow_compare(rule_action, category,
                                 state_path=state_path,
                                 shadow_log=shadow_log)
            logged.append(rec)
        except Exception as e:
            errors += 1
            logging.warning('shadow_compare failed for sku=%s: %s',
                            action.get('sku'), e)
    return {
        'logged': len(logged),
        'skipped': skipped,
        'errors': errors,
        'records': logged,
    }
