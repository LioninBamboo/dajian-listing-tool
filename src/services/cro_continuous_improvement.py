"""S140 — Continuous Improvement.

周/月自动改进建议生成器:
聚合 effect_audit / sentinel / approvals / sales_health 信号 → 输出可执行 actions.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)
SEVERITY_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _safe(fn, default):
    if fn is None:
        return default
    try:
        return fn()
    except Exception:
        logger.warning('CI fetcher failed', exc_info=True)
        return default


def collect_signals(*,
                    traffic_light_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                    returns_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                    lift_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                    cost_fn: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        'traffic_light': _safe(traffic_light_fn, None),
        'returns': _safe(returns_fn, None),
        'lift': _safe(lift_fn, None),
        'cost': _safe(cost_fn, None),
    }


def generate_suggestions(signals: Dict[str, Any]) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []

    traffic = signals.get('traffic_light') or {}
    red_pillars = traffic.get('red_pillars') or traffic.get('red') or []
    if red_pillars:
        suggestions.append({
            'area': 'traffic_light',
            'severity': 'critical',
            'action': 'address red pillar',
            'evidence': ', '.join(str(item) for item in red_pillars),
            'evidence_strength': len(red_pillars),
        })

    returns = signals.get('returns') or {}
    returns_rate = float(returns.get('rate', returns.get('returns_rate', 0.0)) or 0.0)
    if returns_rate > 0.10:
        suggestions.append({
            'area': 'returns',
            'severity': 'high',
            'action': 'investigate quality',
            'evidence': f'returns_rate={returns_rate:.2%}',
            'evidence_strength': max(1, int(round(returns_rate * 100))),
        })

    lift = signals.get('lift') or {}
    trend = str(lift.get('lift_trend') or lift.get('trend') or '').lower()
    lift_delta = float(lift.get('lift_delta', 0.0) or 0.0)
    if trend in {'down', 'declining', 'negative'} or lift_delta < 0:
        suggestions.append({
            'area': 'lift',
            'severity': 'medium',
            'action': 'review experiments',
            'evidence': trend or f'lift_delta={lift_delta:.4f}',
            'evidence_strength': max(1, int(abs(lift_delta) * 100)),
        })

    cost = signals.get('cost') or {}
    over_budget = bool(cost.get('over_budget'))
    burn_multiple = float(cost.get('burn_multiple', 1.0) or 1.0)
    if over_budget or burn_multiple > 1:
        suggestions.append({
            'area': 'cost',
            'severity': 'high',
            'action': 'optimize spend',
            'evidence': ('over_budget' if over_budget
                         else f'burn_multiple={burn_multiple:.2f}'),
            'evidence_strength': max(1, int(round(max(burn_multiple, 1) * 2))),
        })

    return suggestions


def rank_suggestions(suggestions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        suggestions,
        key=lambda item: (
            SEVERITY_RANK.get(item.get('severity', 'low'), 99),
            -int(item.get('evidence_strength', 1) or 1),
            str(item.get('area') or ''),
        ),
    )


def render_weekly_brief(suggestions: List[Dict[str, Any]],
                        llm_call: Optional[Callable[[str], str]] = None) -> str:
    ranked = rank_suggestions(list(suggestions or []))
    lines = ['# Weekly Improvement Brief', '',
             f'Total suggestions: {len(ranked)}', '']
    for item in ranked:
        lines.append(
            f'- [{item["severity"]}] {item["area"]}: {item["action"]} '
            f'({item.get("evidence", "")})'
        )
    if not ranked:
        lines.append('No improvement actions proposed this cycle.')
    fallback = '\n'.join(lines) + '\n'
    if llm_call is None:
        return fallback
    try:
        polished = llm_call(fallback)
        return polished if isinstance(polished, str) and polished.strip() else fallback
    except Exception as e:
        return fallback + f'\n(LLM unavailable: {e})\n'


def _suggest_from_effect(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not stats:
        return out
    improved = float(stats.get('improved_rate', 0.0) or 0.0)
    total = int(stats.get('total', 0) or 0)
    if total >= 20 and improved < 0.30:
        out.append({'area': 'thresholds', 'priority': 'high',
                     'recommendation': 'Relax aggressive thresholds — '
                                        f'improved_rate={improved:.2f} too low'})
    if total >= 20 and improved > 0.65:
        out.append({'area': 'thresholds', 'priority': 'medium',
                     'recommendation': 'Tighten thresholds — '
                                        f'improved_rate={improved:.2f} '
                                        'leaving lift on the table'})
    return out


def _suggest_from_sentinel(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not stats:
        return []
    crit = int(stats.get('critical_count', 0) or 0)
    rec = int(stats.get('recurring_skus', 0) or 0)
    out = []
    if crit >= 5:
        out.append({'area': 'monitoring', 'priority': 'critical',
                     'recommendation': f'{crit} critical alerts in window — '
                                        'review alert ladder + on-call'})
    if rec >= 3:
        out.append({'area': 'root_cause', 'priority': 'high',
                     'recommendation': f'{rec} SKUs recurring in alerts — '
                                        'do RCA before next iteration'})
    return out


def _suggest_from_approvals(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not stats:
        return []
    pending = int(stats.get('pending_count', 0) or 0)
    avg_age = float(stats.get('avg_age_days', 0.0) or 0.0)
    out = []
    if pending >= 50:
        out.append({'area': 'approvals', 'priority': 'high',
                     'recommendation': f'{pending} approvals queued — '
                                        'enable RBAC auto-approve for low-risk'})
    if avg_age >= 7:
        out.append({'area': 'approvals', 'priority': 'medium',
                     'recommendation': f'avg approval age {avg_age:.1f}d — '
                                        'add daily reminder'})
    return out


def _suggest_from_sales(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not stats:
        return []
    decline = int(stats.get('declining_skus', 0) or 0)
    no_sale = int(stats.get('zero_sale_skus', 0) or 0)
    out = []
    if decline >= 10:
        out.append({'area': 'sales_health', 'priority': 'high',
                     'recommendation': f'{decline} SKUs declining — '
                                        'investigate keyword/competition shift'})
    if no_sale >= 20:
        out.append({'area': 'sales_health', 'priority': 'medium',
                     'recommendation': f'{no_sale} SKUs zero-sale — '
                                        'consider delist candidates'})
    return out


def generate_improvements(
    *,
    effect_audit_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
    sentinel_stats_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
    approvals_stats_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
    sales_health_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    suggestions: List[Dict[str, Any]] = []
    suggestions.extend(_suggest_from_effect(_safe(effect_audit_fetcher, {})))
    suggestions.extend(_suggest_from_sentinel(_safe(sentinel_stats_fetcher,
                                                       {})))
    suggestions.extend(_suggest_from_approvals(_safe(approvals_stats_fetcher,
                                                       {})))
    suggestions.extend(_suggest_from_sales(_safe(sales_health_fetcher, {})))
    suggestions.sort(key=lambda s: SEVERITY_RANK.get(s['priority'], 9))
    by_area: Dict[str, int] = {}
    for s in suggestions:
        by_area[s['area']] = by_area.get(s['area'], 0) + 1
    return {
        'generated_at': _utcnow_iso(),
        'count': len(suggestions),
        'suggestions': suggestions,
        'by_area': by_area,
    }


def render_brief(report: Dict[str, Any]) -> str:
    lines = [f'# Continuous Improvement ({report.get("generated_at")})',
              '',
              f'Total suggestions: {report.get("count", 0)}',
              '']
    for s in report.get('suggestions') or []:
        lines.append(f'- [{s["priority"]}] ({s["area"]}) '
                      f'{s["recommendation"]}')
    if not report.get('suggestions'):
        lines.append('No actionable improvements this cycle.')
    return '\n'.join(lines) + '\n'
