"""S79 — cost-aware 路由.

包装 cro_daily_runner_bandit.decide_action: 当今日累计 LLM 成本超预算,
强制走 rule 不调 LLM/bandit; 否则正常走.

使用方式: 上游传 budget_today_usd + cost_summary_callable (返 today_total_usd).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from src.services.cro_cost_meter import (
    DEFAULT_LOG as COST_LOG_DEFAULT, summarise_costs,
)
from src.services.cro_daily_runner_bandit import (
    SHADOW_LOG_DEFAULT, decide_action,
)
from src.services.cro_bandit_runtime import DEFAULT_STATE_PATH


def today_cost_usd(cost_log: Path = COST_LOG_DEFAULT) -> float:
    return float(summarise_costs(log_path=cost_log).get('total_cost', 0.0))


def cost_aware_decide(rule_action: str,
                      category: str,
                      *,
                      budget_today_usd: Optional[float] = None,
                      cost_lookup: Optional[Callable[[], float]] = None,
                      mode: str = 'shadow',
                      state_path: Path = DEFAULT_STATE_PATH,
                      shadow_log: Path = SHADOW_LOG_DEFAULT,
                      cost_log: Path = COST_LOG_DEFAULT,
                      ) -> Tuple[str, Dict[str, Any]]:
    """主入口. 返回 (final_action, meta).
    超预算时强制 rule 模式不写 shadow log."""
    if budget_today_usd is not None:
        try:
            spent = (cost_lookup() if cost_lookup is not None
                     else today_cost_usd(cost_log))
        except Exception:
            spent = 0.0
        if spent >= budget_today_usd:
            return rule_action, {
                'final_source': 'rule',
                'mode_overridden': True,
                'override_reason': 'cost_budget_exceeded',
                'spent_usd': round(spent, 6),
                'budget_usd': budget_today_usd,
            }
    final, meta = decide_action(rule_action, category, mode=mode,
                                state_path=state_path,
                                shadow_log=shadow_log)
    meta['mode_overridden'] = False
    return final, meta


def remaining_budget(budget_today_usd: float,
                     cost_log: Path = COST_LOG_DEFAULT) -> float:
    return max(0.0, budget_today_usd - today_cost_usd(cost_log))
