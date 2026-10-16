"""S80 — cost pillar.

不修改 cro_dashboard_traffic_light; 提供 cost_pillar(today_cost, daily_budget)
返回与现有 pillar 兼容的 dict, 让 cro_status 页加第 6 张卡.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.services.cro_cost_meter import (
    DEFAULT_LOG, downgrade_recommendation, summarise_costs,
)


def status_for_cost_ratio(ratio: float) -> str:
    if ratio >= 1.0:
        return 'red'
    if ratio >= 0.80:
        return 'yellow'
    return 'green'


def cost_pillar(*,
                today_cost_usd: Optional[float] = None,
                daily_budget_usd: float = 5.0,
                cost_log_path=DEFAULT_LOG,
                downgrade_threshold_avg_usd: float = 0.005,
                ) -> Dict[str, Any]:
    if today_cost_usd is None:
        summary = summarise_costs(log_path=cost_log_path)
        today_cost_usd = float(summary.get('total_cost', 0.0))
    else:
        summary = {'total_cost': today_cost_usd, 'count': 0,
                   'avg_cost_per_decision': 0.0,
                   'by_action': {}}

    used_ratio = (today_cost_usd / daily_budget_usd
                  if daily_budget_usd > 0 else 0.0)
    status = status_for_cost_ratio(used_ratio)
    rec = downgrade_recommendation(summary,
                                   threshold_avg_usd=downgrade_threshold_avg_usd)
    msg = (
        f'今日累计 ${today_cost_usd:.4f} / 预算 ${daily_budget_usd:.2f} '
        f'({used_ratio * 100:.1f}%)'
    )
    if rec.get('suggest_downgrade_to_rules'):
        msg += ' · 建议降级到规则模式'
    return {
        'pillar': 'cost',
        'status': status,
        'today_cost_usd': round(today_cost_usd, 6),
        'daily_budget_usd': daily_budget_usd,
        'used_ratio': round(used_ratio, 4),
        'avg_cost_per_decision':
            summary.get('avg_cost_per_decision', 0.0),
        'suggest_downgrade': rec.get('suggest_downgrade_to_rules', False),
        'message': msg,
    }


def merge_into_traffic_light(traffic_light: Dict[str, Any],
                             cost_pillar_result: Dict[str, Any],
                             ) -> Dict[str, Any]:
    """把 cost pillar 加入 traffic_light dict, 重算 overall."""
    pillars = list(traffic_light.get('pillars') or [])
    pillars.append(cost_pillar_result)
    order = {'green': 0, 'yellow': 1, 'red': 2}
    overall = max((p.get('status', 'green') for p in pillars),
                  key=lambda s: order.get(s, 0))
    return {
        **traffic_light,
        'pillars': pillars,
        'status_overall': overall,
    }
