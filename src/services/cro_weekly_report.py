"""S53 — CRO 周报自动生成.

每周一 09:00 拼装一份中文周报: cohort lift / approval rate / blacklist 命中 / 退货 top10
/ 库存 alert 数. 走 S39 NL brief 风格, 邮件/钉钉发出由调用方挂钩 (sender 注入)。

数据源全部 fetcher 注入 → 测试用 in-memory dict 即可。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, List, Optional


def build_weekly_report(
    week_label: str,
    *,
    cohort_fetcher: Callable[[], Dict[str, Any]],
    approval_fetcher: Callable[[], Dict[str, Any]],
    blacklist_fetcher: Callable[[], Dict[str, Any]],
    returns_fetcher: Callable[[], Dict[str, Any]],
    inventory_fetcher: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    coh = cohort_fetcher() or {}
    appr = approval_fetcher() or {}
    bl = blacklist_fetcher() or {}
    rt = returns_fetcher() or {}
    inv = inventory_fetcher() or {}

    return {
        'week': week_label,
        'cohort': {
            'control_sold': coh.get('control_sold', 0),
            'treatment_sold': coh.get('treatment_sold', 0),
            'lift_pct': _lift(coh.get('control_sold', 0),
                              coh.get('treatment_sold', 0)),
        },
        'approvals': {
            'total': appr.get('total', 0),
            'approved': appr.get('approved', 0),
            'rejected': appr.get('rejected', 0),
            'approval_rate': _rate(appr.get('approved', 0),
                                   appr.get('total', 0)),
        },
        'blacklist': {
            'added_this_week': bl.get('added', 0),
            'total': bl.get('total', 0),
        },
        'returns_top10': (rt.get('high_return') or [])[:10],
        'inventory_alerts': inv.get('throttle_count', 0),
    }


def _lift(ctrl: float, treat: float) -> float:
    if not ctrl:
        return 0.0
    return round((treat - ctrl) / ctrl, 4)


def _rate(num: int, denom: int) -> float:
    if not denom:
        return 0.0
    return round(num / denom, 3)


def render_weekly_text(report: Dict[str, Any]) -> str:
    coh = report['cohort']
    appr = report['approvals']
    bl = report['blacklist']
    lines: List[str] = [
        f'📊 CRO 周报 — {report["week"]}',
        '',
        f'A/B cohort: control 售出 {coh["control_sold"]} 件, '
        f'treatment {coh["treatment_sold"]} 件, '
        f'lift {coh["lift_pct"] * 100:.1f}%',
        f'审批: {appr["approved"]}/{appr["total"]} 通过 '
        f'(通过率 {appr["approval_rate"] * 100:.0f}%, 拒绝 {appr["rejected"]})',
        f'黑名单: 本周新增 {bl["added_this_week"]} 个, '
        f'累计 {bl["total"]} 个',
        f'库存预警: {report["inventory_alerts"]} 个 SKU',
    ]
    rt = report.get('returns_top10') or []
    if rt:
        lines.append('')
        lines.append(f'退货 Top {len(rt)}:')
        for r in rt:
            lines.append(
                f'  • {r.get("sku")}: '
                f'{(r.get("return_rate") or 0) * 100:.1f}% '
                f'({r.get("return_count", 0)}/{r.get("sold_count", 0)})'
            )
    return '\n'.join(lines)


def send_weekly_report(
    report: Dict[str, Any],
    sender: Optional[Callable[[str, str], Any]] = None,
    subject_prefix: str = 'CRO 周报',
) -> bool:
    if sender is None:
        return False
    subject = f'{subject_prefix} — {report.get("week", "")}'
    body = render_weekly_text(report)
    try:
        sender(subject, body)
        return True
    except Exception:
        return False


def current_iso_week_label(today: Optional[date] = None) -> str:
    today = today or date.today()
    y, w, _ = today.isocalendar()
    return f'{y}-W{w:02d}'
