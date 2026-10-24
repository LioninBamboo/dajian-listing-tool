"""S77 — cohort 健康监控.

输入: weekly aggregates by cohort (control/treatment)
输出: 每周 lift, 趋势 (improving/flat/worsening), 是否触发 STOP_AB.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

CONTINUOUS_NEGATIVE_WEEKS_TO_STOP = 3
NEGATIVE_LIFT_THRESHOLD = -0.05    # < -5% → 算"负"
IMPROVE_LIFT_THRESHOLD = 0.05      # > 5% → 算"涨"


def _safe_lift(treatment: float, control: float) -> Optional[float]:
    if control is None or control <= 0:
        return None
    return (treatment - control) / control


def compute_weekly_lift(rows: Iterable[Dict[str, Any]],
                        metric: str = 'sold_qty',
                        ) -> List[Dict[str, Any]]:
    """rows: [{week, cohort, sold_qty}, ...]
    返回: [{week, control, treatment, lift_pct}, ...] 按 week 排序."""
    by_week: Dict[str, Dict[str, float]] = {}
    for r in rows:
        week = r.get('week')
        cohort = r.get('cohort')
        if not week or cohort not in {'control', 'treatment'}:
            continue
        slot = by_week.setdefault(week, {'control': 0.0, 'treatment': 0.0})
        slot[cohort] += float(r.get(metric, 0) or 0)
    out: List[Dict[str, Any]] = []
    for week in sorted(by_week.keys()):
        c = by_week[week]['control']
        t = by_week[week]['treatment']
        lift = _safe_lift(t, c)
        out.append({
            'week': week,
            'control': c,
            'treatment': t,
            'lift_pct': round(lift, 4) if lift is not None else None,
        })
    return out


def trend(weekly: List[Dict[str, Any]]) -> str:
    valid = [w['lift_pct'] for w in weekly if w['lift_pct'] is not None]
    if not valid:
        return 'unknown'
    last = valid[-1]
    if last >= IMPROVE_LIFT_THRESHOLD:
        return 'improving'
    if last <= NEGATIVE_LIFT_THRESHOLD:
        return 'worsening'
    return 'flat'


def should_stop_ab(weekly: List[Dict[str, Any]],
                   negative_weeks: int = CONTINUOUS_NEGATIVE_WEEKS_TO_STOP,
                   ) -> Dict[str, Any]:
    valid = [w for w in weekly if w['lift_pct'] is not None]
    if len(valid) < negative_weeks:
        return {'stop': False, 'reason': 'insufficient_data',
                'weeks_examined': len(valid)}
    tail = valid[-negative_weeks:]
    all_negative = all(w['lift_pct'] <= NEGATIVE_LIFT_THRESHOLD
                       for w in tail)
    if all_negative:
        return {'stop': True,
                'reason': f'连续 {negative_weeks} 周 lift_pct ≤ '
                          f'{NEGATIVE_LIFT_THRESHOLD}',
                'tail_lifts': [w['lift_pct'] for w in tail]}
    return {'stop': False, 'reason': 'trend_not_persistently_negative',
            'tail_lifts': [w['lift_pct'] for w in tail]}


def health_report(rows: Iterable[Dict[str, Any]],
                  metric: str = 'sold_qty',
                  ) -> Dict[str, Any]:
    weekly = compute_weekly_lift(rows, metric)
    return {
        'weekly': weekly,
        'trend': trend(weekly),
        'stop': should_stop_ab(weekly),
        'metric': metric,
    }
