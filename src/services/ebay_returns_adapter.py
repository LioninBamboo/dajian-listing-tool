"""S56 — eBay Returns API 适配器.

负责把 eBay Sell Fulfillment Returns API 的原始响应转成
src/services/cro_returns_feedback.analyze_returns 期望的格式:
  [{sku, return_count, sold_count, reason_codes:[...]}]

http_get(path, params) 由调用方注入 (生产可绑定 real_ebay_client._authed_get,
测试可塞 dict-mock)。这样模块本身可单测, 不强依赖 OAuth。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

RETURNS_PATH = '/sell/fulfillment/v1/return'
DEFAULT_LIMIT = 200


def fetch_returns_raw(http_get: Callable[[str, Dict[str, Any]], Dict[str, Any]],
                      days: int = 30,
                      limit: int = DEFAULT_LIMIT,
                      ) -> List[Dict[str, Any]]:
    """拉单页 (调用方负责分页/速率)."""
    params = {'limit': limit, 'lookback_days': days}
    data = http_get(RETURNS_PATH, params) or {}
    return data.get('returns') or []


def aggregate_by_sku(raw_returns: List[Dict[str, Any]],
                     sold_lookup: Optional[Callable[[str], int]] = None,
                     ) -> List[Dict[str, Any]]:
    """聚合单条 returns → 按 sku 汇总."""
    by_sku: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {'return_count': 0, 'reason_codes': []}
    )
    for r in raw_returns:
        sku = _extract_sku(r)
        if not sku:
            continue
        slot = by_sku[sku]
        slot['return_count'] += 1
        reason = (r.get('reason') or r.get('reasonCode') or '').upper()
        if reason:
            slot['reason_codes'].append(reason)
    out: List[Dict[str, Any]] = []
    for sku, slot in by_sku.items():
        sold = sold_lookup(sku) if sold_lookup else 0
        out.append({
            'sku': sku,
            'return_count': slot['return_count'],
            'sold_count': sold,
            'reason_codes': slot['reason_codes'],
        })
    return out


def _extract_sku(record: Dict[str, Any]) -> Optional[str]:
    # eBay 不同 API 路径下 sku 可能位于不同字段
    for key in ('sku', 'itemSKU', 'lineItemSku'):
        v = record.get(key)
        if v:
            return str(v)
    item = record.get('lineItems') or []
    if isinstance(item, list) and item and isinstance(item[0], dict):
        return item[0].get('sku') or item[0].get('itemSKU')
    return None


def returns_fetcher_factory(
    http_get: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    sold_lookup: Optional[Callable[[str], int]] = None,
    days: int = 30,
) -> Callable[[], List[Dict[str, Any]]]:
    """返回 cro_returns_feedback.analyze_returns 可直接用的 fetcher."""
    def _fetch() -> List[Dict[str, Any]]:
        try:
            raw = fetch_returns_raw(http_get, days=days)
        except Exception:
            return []
        return aggregate_by_sku(raw, sold_lookup=sold_lookup)
    return _fetch
