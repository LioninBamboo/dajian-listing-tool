"""S121 — 生产编排器.

把多个分析模块的 actionable items 统一封装成 cro_action_queue 的 row dict.
不直接写 sqlite, 只产出 row list, 供调用方 (S123 run_full_loop) 入队.
所有 fetcher/analyzer 都可注入, 默认空, 便于测试与降级.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

# 模块名→action_type 映射
_KIND_ACTION = {
    'returns_prevention': 'pause_candidate',     # 走 review/delist
    'keyword_gap': 'title_insert',
    'search_query_match': 'title_insert',
    'cross_category_arb': 'category_migrate',
    'pricing_psychology': 'price_charm',
    'supplier_risk': 'supplier_review',
    'image_text_extract': 'image_refresh',
}


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _row(sku: str, action: str, payload: Dict[str, Any],
         signals: List[str]) -> Dict[str, Any]:
    return {
        'sku': sku,
        'action_type': action,
        'enqueued_at': _now_iso(),
        'status': 'pending',
        'payload': payload,
        'signals': signals,
        'source': 'cro_production_wire',
    }


def from_returns_prevention(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for it in report.get('pause_candidates', []) or []:
        rows.append(_row(it.get('sku'), _KIND_ACTION['returns_prevention'],
                          {'reason': it.get('reason'),
                           'fix_hint': it.get('fix_hint'),
                           'return_rate': it.get('return_rate')},
                          ['returns']))
    for it in report.get('review_candidates', []) or []:
        rows.append(_row(it.get('sku'), 'review',
                          {'reason': it.get('reason'),
                           'return_rate': it.get('return_rate')},
                          ['returns']))
    return rows


def from_search_query_match(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for it in report.get('items', []) or []:
        inserts = it.get('recommended_inserts') or []
        if not inserts:
            continue
        rows.append(_row(it.get('sku'),
                          _KIND_ACTION['search_query_match'],
                          {'inserts': inserts,
                           'coverage': it.get('coverage')},
                          ['search_match']))
    return rows


def from_keyword_gap(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for it in report.get('items', []) or []:
        if it.get('overlap_ratio', 1.0) >= 0.50:
            continue
        inserts = it.get('recommended_inserts') or []
        if not inserts:
            continue
        rows.append(_row(it.get('sku'),
                          _KIND_ACTION['keyword_gap'],
                          {'inserts': inserts,
                           'overlap_ratio': it.get('overlap_ratio')},
                          ['keyword_gap']))
    return rows


def from_cross_category_arb(opportunities: List[Dict[str, Any]]
                              ) -> List[Dict[str, Any]]:
    rows = []
    for o in opportunities or []:
        rows.append(_row(o.get('sku'),
                          _KIND_ACTION['cross_category_arb'],
                          {'target_category': o.get('target_category'),
                           'recommended_price': o.get('recommended_price'),
                           'lift': o.get('lift')},
                          ['arbitrage']))
    return rows


def from_supplier_risk(report: Dict[str, Any],
                        sku_to_supplier: Dict[str, str] = None
                        ) -> List[Dict[str, Any]]:
    rows = []
    sku_to_supplier = sku_to_supplier or {}
    high_risk_ids = {it['supplier_id'] for it in report.get('items', [])
                     if it.get('risk_label') in ('high', 'critical')}
    for sku, sup_id in sku_to_supplier.items():
        if sup_id in high_risk_ids:
            rows.append(_row(sku, _KIND_ACTION['supplier_risk'],
                              {'supplier_id': sup_id},
                              ['supplier_risk']))
    return rows


def _safe_call(fn, default):
    if fn is None:
        return default
    try:
        return fn()
    except Exception:
        return default


def build_pipeline(
    *,
    returns_report_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    search_match_report_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    keyword_gap_report_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    arb_opportunities_fn: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    supplier_risk_report_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    sku_to_supplier_fn: Optional[Callable[[], Dict[str, str]]] = None,
    blacklist: Iterable[str] = None,
) -> Dict[str, Any]:
    """聚合多源 actionable items, 去重 (sku, action_type)."""
    bl = set(blacklist or [])
    rows: List[Dict[str, Any]] = []

    rows.extend(from_returns_prevention(_safe_call(returns_report_fn, {})))
    rows.extend(from_search_query_match(_safe_call(search_match_report_fn, {})))
    rows.extend(from_keyword_gap(_safe_call(keyword_gap_report_fn, {})))
    rows.extend(from_cross_category_arb(_safe_call(arb_opportunities_fn, [])))
    rows.extend(from_supplier_risk(
        _safe_call(supplier_risk_report_fn, {}),
        _safe_call(sku_to_supplier_fn, {}),
    ))

    # 去重 + blacklist 过滤
    seen = set()
    deduped: List[Dict[str, Any]] = []
    skipped_blacklisted = 0
    skipped_dup = 0
    for r in rows:
        sku = r.get('sku')
        if not sku:
            continue
        if sku in bl:
            skipped_blacklisted += 1
            continue
        key = (sku, r['action_type'])
        if key in seen:
            skipped_dup += 1
            continue
        seen.add(key)
        deduped.append(r)

    by_action: Dict[str, int] = {}
    for r in deduped:
        by_action[r['action_type']] = by_action.get(r['action_type'], 0) + 1

    return {
        'rows': deduped,
        'count': len(deduped),
        'by_action': by_action,
        'skipped_blacklisted': skipped_blacklisted,
        'skipped_duplicate': skipped_dup,
    }
