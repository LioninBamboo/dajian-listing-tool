"""S83 — cohort drift 检测.

监控 control 组绝对销量/CTR 等的环比变化, 提前发现外部环境扰动
(节假日/算法变更), 避免把环境扰动误认为 treatment 效果.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

DEFAULT_DRIFT_THRESHOLD = 0.20  # 20% 环比变化即告警


def _pct_change(current: float, previous: float) -> float:
    if previous is None or previous <= 0:
        return 0.0
    return (current - previous) / previous


def detect_drift(history: Iterable[Dict[str, Any]],
                 *,
                 metric: str = 'control_value',
                 threshold: float = DEFAULT_DRIFT_THRESHOLD,
                 ) -> Dict[str, Any]:
    """history: [{week, control_value}, ...] 按 week 升序自动排序."""
    rows = sorted(
        [r for r in history if r.get('week') is not None
         and r.get(metric) is not None],
        key=lambda r: r['week'],
    )
    if len(rows) < 2:
        return {'drift_detected': False, 'reason': 'insufficient_history',
                'changes': []}
    changes: List[Dict[str, Any]] = []
    drift_weeks: List[str] = []
    for i in range(1, len(rows)):
        prev = float(rows[i - 1][metric])
        cur = float(rows[i][metric])
        pct = _pct_change(cur, prev)
        flagged = abs(pct) >= threshold
        changes.append({
            'week': rows[i]['week'],
            'previous': prev,
            'current': cur,
            'pct_change': round(pct, 4),
            'drift': flagged,
        })
        if flagged:
            drift_weeks.append(rows[i]['week'])
    return {
        'drift_detected': bool(drift_weeks),
        'drift_weeks': drift_weeks,
        'changes': changes,
        'threshold': threshold,
        'metric': metric,
    }


def annotate_lift_with_drift(weekly_lift: List[Dict[str, Any]],
                             drift_result: Dict[str, Any],
                             ) -> List[Dict[str, Any]]:
    """把 drift_weeks 标到 cohort_health.compute_weekly_lift 输出上."""
    flagged = set(drift_result.get('drift_weeks') or [])
    out = []
    for row in weekly_lift:
        marker = row.get('week') in flagged
        out.append({**row, 'control_drift': marker})
    return out


def summarise_drift(drift_result: Dict[str, Any]) -> str:
    if not drift_result.get('drift_detected'):
        return 'control 组稳定, 可信任 lift 信号'
    weeks = drift_result.get('drift_weeks') or []
    return (f'control 组在 {len(weeks)} 周发生显著漂移 ({", ".join(weeks)}); '
            f'建议人工排查环境因素后再判断 treatment 效果')
