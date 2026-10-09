"""S52 — 跨 slice 自动联动 orchestrator.

把 S48 退货分析、S49 库存 throttle、S29 黑名单、S27 库存过滤 串成真闭环:

  1) 跑 S48 analyze_returns → 拿 blacklist_skus → 调 S29 add_to_blacklist
  2) 跑 S49 evaluate → 拿 throttle_skus → 输出可被 S27 cro_inventory_filter 用的 set
  3) 返回 summary 报告供 dashboard / 周报使用

依赖通过参数注入避免硬耦合。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def chain_returns_to_blacklist(
    returns_report: Dict[str, Any],
    add_blacklist: Callable[[str, str], Any],
    reason_prefix: str = 'auto-S48',
) -> List[str]:
    skus = list(returns_report.get('blacklist_skus') or [])
    added: List[str] = []
    for sku in skus:
        try:
            add_blacklist(sku, f'{reason_prefix}: high return rate')
            added.append(sku)
        except Exception:
            continue
    return added


def chain_inventory_throttle_to_filter(
    throttle_report: Dict[str, Any],
) -> set:
    return set(throttle_report.get('throttle_skus') or [])


def is_actionable(
    sku: str,
    blacklisted: set,
    throttled: set,
    action: str,
) -> bool:
    """单点查询: 给定 sku + action, 是否仍可执行."""
    if sku in blacklisted:
        return False
    if action in ('promote', 'price_drop') and sku in throttled:
        return False
    return True


def run_chain(
    *,
    returns_analyzer: Callable[[], Dict[str, Any]],
    inventory_evaluator: Callable[[], Dict[str, Any]],
    add_blacklist: Optional[Callable[[str, str], Any]] = None,
) -> Dict[str, Any]:
    rt = returns_analyzer() or {}
    inv = inventory_evaluator() or {}
    added = (chain_returns_to_blacklist(rt, add_blacklist)
             if add_blacklist else [])
    throttled = chain_inventory_throttle_to_filter(inv)
    return {
        'returns_high_count': rt.get('high_return_count', 0),
        'blacklist_added': added,
        'blacklist_added_count': len(added),
        'throttle_skus': sorted(throttled),
        'throttle_count': len(throttled),
    }
