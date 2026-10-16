"""S137 — Capacity Planning.

线性外推磁盘/DB/队列容量耗尽日, 给出预警等级.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _slope_per_day(samples: List[Dict[str, Any]]) -> Optional[float]:
    """对 [{day_index, value}] 做最小二乘斜率估计."""
    if not samples or len(samples) < 2:
        return None
    n = len(samples)
    xs = [float(s['day_index']) for s in samples]
    ys = [float(s['value']) for s in samples]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    return num / den


def project_exhaustion(
    *,
    samples: List[Dict[str, Any]],
    capacity: float,
    current_value: float,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """samples 是按时间序的 day_index/value 点; 返回耗尽日期估计."""
    if capacity <= 0:
        return {'status': 'invalid_capacity'}
    now = now or datetime.now(UTC).replace(tzinfo=None)
    used_pct = current_value / capacity if capacity > 0 else 0.0
    slope = _slope_per_day(samples)
    if slope is None or slope <= 0:
        return {'status': 'no_growth' if (slope is not None and slope <= 0)
                          else 'insufficient_data',
                'used_pct': round(used_pct, 4),
                'slope_per_day': slope}
    remaining = max(0.0, capacity - current_value)
    days_left = remaining / slope
    eta = now + timedelta(days=days_left)

    # 等级
    if days_left <= 7:
        severity = 'critical'
    elif days_left <= 30:
        severity = 'high'
    elif days_left <= 90:
        severity = 'medium'
    else:
        severity = 'low'

    return {
        'status': 'projected',
        'used_pct': round(used_pct, 4),
        'slope_per_day': slope,
        'days_until_full': round(days_left, 1),
        'eta': eta.isoformat(),
        'severity': severity,
    }


def assess_components(components: Dict[str, Dict[str, Any]],
                       *, now: Optional[datetime] = None
                       ) -> Dict[str, Any]:
    """
    components: { name: {samples, capacity, current_value} }
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)
    by_component: Dict[str, Dict[str, Any]] = {}
    severity_rank = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    worst = 'low'
    worst_rank = severity_rank['low']
    critical_components: List[str] = []

    for name, c in (components or {}).items():
        res = project_exhaustion(
            samples=c.get('samples') or [],
            capacity=float(c.get('capacity') or 0.0),
            current_value=float(c.get('current_value') or 0.0),
            now=now,
        )
        by_component[name] = res
        sev = res.get('severity')
        if sev and severity_rank.get(sev, 99) < worst_rank:
            worst_rank = severity_rank[sev]
            worst = sev
        if sev == 'critical':
            critical_components.append(name)

    return {
        'by_component': by_component,
        'worst_severity': worst,
        'critical_components': critical_components,
    }
