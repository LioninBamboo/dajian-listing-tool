"""S50 — 端到端 traffic-light dashboard.

聚合 S26-S49 的关键信号到一屏 (绿/黄/红 + 一句话总结)。
所有 fetcher 注入 → 测试可不依赖真实 DB / 文件。

输出形如:
{
  status_overall: 'green' | 'yellow' | 'red',
  pillars: [
    {name, status, headline, detail_count},
    ...
  ],
  recommended_action: '...一句话...'
}
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# 阈值
RED_HIGH_RETURN = 5
RED_THROTTLE = 10
YELLOW_PENDING_APPROVALS = 10
RED_PENDING_APPROVALS = 30
YELLOW_HIGH_PRIORITY_ALERTS = 5
RED_HIGH_PRIORITY_ALERTS = 15


def _status(value: int, yellow: int, red: int) -> str:
    if value >= red:
        return 'red'
    if value >= yellow:
        return 'yellow'
    return 'green'


def _worst(statuses: List[str]) -> str:
    order = {'green': 0, 'yellow': 1, 'red': 2}
    return max(statuses, key=lambda s: order.get(s, 0)) if statuses else 'green'


def build_dashboard(
    *,
    approvals_fetcher: Callable[[], Dict[str, Any]],
    alerts_fetcher: Callable[[], Dict[str, Any]],
    returns_fetcher: Callable[[], Dict[str, Any]],
    inventory_fetcher: Callable[[], Dict[str, Any]],
    funnel_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    pillars: List[Dict[str, Any]] = []

    # Pillar 1: 待审批
    appr = approvals_fetcher() or {}
    n_appr = appr.get('total', 0) or 0
    pillars.append({
        'name': 'approvals',
        'status': _status(n_appr, YELLOW_PENDING_APPROVALS,
                          RED_PENDING_APPROVALS),
        'headline': f'{n_appr} 条待审批',
        'detail_count': n_appr,
    })

    # Pillar 2: 高优告警 (聚类)
    alerts = alerts_fetcher() or {}
    high_pri = alerts.get('high_priority_count', 0) or 0
    pillars.append({
        'name': 'alerts',
        'status': _status(high_pri, YELLOW_HIGH_PRIORITY_ALERTS,
                          RED_HIGH_PRIORITY_ALERTS),
        'headline': f'{high_pri} 条品类级告警',
        'detail_count': high_pri,
    })

    # Pillar 3: 退货
    rts = returns_fetcher() or {}
    n_ret = rts.get('high_return_count', 0) or 0
    pillars.append({
        'name': 'returns',
        'status': 'red' if n_ret >= RED_HIGH_RETURN
        else ('yellow' if n_ret > 0 else 'green'),
        'headline': f'{n_ret} 个高退货 SKU',
        'detail_count': n_ret,
    })

    # Pillar 4: 库存 throttle
    inv = inventory_fetcher() or {}
    n_thr = len(inv.get('throttle_skus', []) or [])
    pillars.append({
        'name': 'inventory',
        'status': 'red' if n_thr >= RED_THROTTLE
        else ('yellow' if n_thr > 0 else 'green'),
        'headline': f'{n_thr} 个 SKU 库存预警',
        'detail_count': n_thr,
    })

    # Pillar 5: 漏斗 (可选)
    if funnel_fetcher is not None:
        f = funnel_fetcher() or {}
        unhealthy = f.get('unhealthy_count', 0) or 0
        pillars.append({
            'name': 'funnel',
            'status': 'red' if unhealthy >= 50
            else ('yellow' if unhealthy >= 10 else 'green'),
            'headline': f'{unhealthy} 个 SKU 漏斗异常',
            'detail_count': unhealthy,
        })

    overall = _worst([p['status'] for p in pillars])
    rec = _recommend(overall, pillars)
    return {
        'status_overall': overall,
        'pillars': pillars,
        'recommended_action': rec,
    }


def _recommend(overall: str, pillars: List[Dict[str, Any]]) -> str:
    if overall == 'green':
        return '系统平稳, 无需立即操作'
    # 找最严重的那块
    red_pillars = [p for p in pillars if p['status'] == 'red']
    target = red_pillars[0] if red_pillars else next(
        (p for p in pillars if p['status'] == 'yellow'),
        pillars[0] if pillars else None,
    )
    if target is None:
        return '无可用诊断'
    name = target['name']
    hint = {
        'approvals': '前往 /cro_loop 审批积压项',
        'alerts': '查看品类级聚类报告, 优先处理高优 macro_drop',
        'returns': '审计高退货 SKU, 必要时加入黑名单',
        'inventory': '通知采购补货, 暂停受影响 SKU 的 promote',
        'funnel': '按漏斗阶段诊断: 低 CTR 改图/标题, 低 watch 改价',
    }.get(name, '人工排查')
    return f'优先处理 {name}: {hint}'


def render_traffic_light(report: Dict[str, Any]) -> str:
    icon = {'green': '🟢', 'yellow': '🟡', 'red': '🔴'}
    overall = report.get('status_overall', 'green')
    lines = [f'{icon[overall]} 整体: {overall.upper()}']
    for p in report.get('pillars', []):
        lines.append(
            f'  {icon[p["status"]]} {p["name"]}: {p["headline"]}'
        )
    lines.append(f'➡ {report.get("recommended_action", "")}')
    return '\n'.join(lines)
