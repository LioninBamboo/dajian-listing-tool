"""S90 — SLO / error budget 看板.

输入: 事件日志 (success/failure 计数 + uptime samples).
输出: 当前 SLI vs SLO, 剩余 error budget, 是否冻结发布.

事件 jsonl 格式: {ts, kind in {request,probe}, success:bool, error?:str}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

DEFAULT_LOG = Path('logs/cro_slo_events.jsonl')
DEFAULT_SLO = 0.99  # 99% success
DEFAULT_BUDGET_WINDOW_DAYS = 30


def _load_events(log_path: Path) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def compute_sli(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    total = 0
    success = 0
    for e in events:
        total += 1
        if bool(e.get('success', False)):
            success += 1
    rate = (success / total) if total > 0 else 1.0
    return {
        'total': total,
        'success': success,
        'failure': total - success,
        'sli': round(rate, 6),
    }


def compute_error_budget(sli_result: Dict[str, Any],
                         slo: float = DEFAULT_SLO) -> Dict[str, Any]:
    total = sli_result.get('total', 0)
    if total == 0:
        return {'budget_total': 0, 'budget_consumed': 0,
                'budget_remaining': 0, 'budget_remaining_pct': 1.0,
                'over_budget': False, 'slo': slo}
    allowed_failures = total * (1.0 - slo)
    consumed = sli_result.get('failure', 0)
    remaining = max(0.0, allowed_failures - consumed)
    pct = (remaining / allowed_failures) if allowed_failures > 0 else 0.0
    return {
        'budget_total': round(allowed_failures, 4),
        'budget_consumed': consumed,
        'budget_remaining': round(remaining, 4),
        'budget_remaining_pct': round(pct, 4),
        'over_budget': consumed > allowed_failures,
        'slo': slo,
    }


def freeze_recommendation(error_budget: Dict[str, Any]) -> Dict[str, Any]:
    if error_budget.get('over_budget'):
        return {'freeze': True, 'severity': 'red',
                'reason': 'error budget exhausted, freeze releases'}
    pct = error_budget.get('budget_remaining_pct', 1.0)
    if pct < 0.10:
        return {'freeze': True, 'severity': 'red',
                'reason': 'error budget < 10%, freeze releases'}
    if pct < 0.25:
        return {'freeze': False, 'severity': 'yellow',
                'reason': 'error budget < 25%, slow rollout'}
    return {'freeze': False, 'severity': 'green',
            'reason': 'budget healthy'}


def slo_dashboard(*,
                  log_path: Path = DEFAULT_LOG,
                  slo: float = DEFAULT_SLO,
                  ) -> Dict[str, Any]:
    events = _load_events(log_path)
    sli = compute_sli(events)
    budget = compute_error_budget(sli, slo=slo)
    advice = freeze_recommendation(budget)
    return {
        'sli': sli,
        'error_budget': budget,
        'recommendation': advice,
        'window_events': len(events),
    }
