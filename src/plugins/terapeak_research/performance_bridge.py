"""
F6 — Competition-monitor → Market-Intelligence performance bridge.

The 竞争监控 dashboard (`src/web/pages/competition_monitor.py`) already
populates `_performance_cache.json` (impressions / views / transactions /
sales by listing_id and SKU) via `EbayPerformanceService`. Until F6 the
Market Intelligence plugin ignored that cache and could only use the
Browse-only "demand_signal" estimate.

This bridge surfaces the seller's own measured performance per SKU so
`opportunity_score()` can be fed `real_str` (max 30 pts) instead of the
estimated_str cap (18 pts), and so the MI UI can show "实测 STR" badges.

The bridge is intentionally pure-read: it never refreshes the cache. If
the cache is stale or missing, `load_seller_performance_index()` returns
an empty dict and callers fall back to the estimated demand signal.
"""
from __future__ import annotations

from typing import Dict, Optional

# Listings need at least this many impressions before we trust their
# transactions/impressions ratio as a real Sell-Through proxy. Below
# this floor the divisor is too small and noise dominates.
MIN_IMPRESSIONS_FOR_REAL_STR = 50


def compute_seller_str(impressions: int, transactions: int, sold_qty: int = 0) -> Optional[float]:
    """
    Return seller-measured Sell-Through Rate as a 0-100 percentage,
    or None when impressions are below the noise floor.

    F6.1 — window alignment: impressions come from Analytics traffic_report
    over a 30-day window, while ``sold_qty`` originates from the Fulfillment
    API which the competition-monitor pulls over 90 days. Mixing them via
    ``max(transactions, sold_qty)`` would inflate STR up to 3x. We therefore
    use ONLY ``transactions`` (30-day, same window as impressions). The
    ``sold_qty`` parameter is kept for the deprecated callers but ignored
    in the math; it is still surfaced separately for display + has_sales.
    """
    try:
        imp = int(impressions or 0)
        tx = int(transactions or 0)
    except (TypeError, ValueError):
        return None
    if imp < MIN_IMPRESSIONS_FOR_REAL_STR:
        return None
    return round((tx / imp) * 100, 2)


def load_seller_performance_index(max_age_hours: int = 24) -> Dict[str, Dict]:
    """
    Build a SKU-keyed lookup of measured performance.

    Returns {} when the cache is missing or older than max_age_hours.
    Each entry contains:
        impressions, views, transactions, sold_qty, str_pct, has_sales,
        listing_id, fetched_at
    """
    try:
        from src.services.ebay_performance import load_performance_cache
    except Exception:
        return {}

    cache = load_performance_cache(max_age_hours=max_age_hours)
    if not cache:
        return {}

    traffic = cache.get('traffic', {}) or {}
    sales = cache.get('sales', {}) or {}
    by_sku = sales.get('by_sku', {}) or {}
    by_listing = sales.get('by_listing', {}) or {}
    fetched_at = cache.get('fetched_at', '')

    # Pivot traffic (keyed by listing_id) into a SKU-keyed view by joining
    # via by_listing[listing_id]['sku']. Listings without a known SKU are
    # skipped because MI evaluates by SKU.
    index: Dict[str, Dict] = {}
    for listing_id, t in traffic.items():
        sku = (by_listing.get(listing_id) or {}).get('sku') or ''
        if not sku:
            continue
        impressions = int(t.get('impressions', 0) or 0)
        views = int(t.get('views', 0) or 0)
        transactions = int(t.get('transactions', 0) or 0)
        sold_qty = int((by_sku.get(sku) or {}).get('qty', 0) or 0)
        if not sold_qty:
            sold_qty = int((by_listing.get(listing_id) or {}).get('qty', 0) or 0)
        # F6.1 — STR computed from transactions only (30d window-aligned).
        # sold_qty is 90d and shown separately; do not feed it to compute_seller_str.
        str_pct = compute_seller_str(impressions, transactions)
        index[sku] = {
            'listing_id': listing_id,
            'impressions': impressions,
            'views': views,
            'transactions': transactions,
            'sold_qty': sold_qty,
            'str_pct': str_pct,
            'has_sales': (sold_qty > 0) or (transactions > 0),
            'fetched_at': fetched_at,
        }

    # Also surface SKUs that sold but had no traffic record (Analytics API
    # returns at most 200 listings, so sales without traffic do happen).
    for sku, s in by_sku.items():
        if sku in index:
            continue
        sold_qty = int(s.get('qty', 0) or 0)
        if sold_qty <= 0:
            continue
        index[sku] = {
            'listing_id': '',
            'impressions': 0,
            'views': 0,
            'transactions': 0,
            'sold_qty': sold_qty,
            'str_pct': None,  # No impressions denominator
            'has_sales': True,
            'fetched_at': fetched_at,
        }

    return index


def coverage_summary(index: Dict[str, Dict], total_candidates: int) -> Dict:
    """Used by the MI sidebar to render '已刊 SKU 实测覆盖率'."""
    if total_candidates <= 0:
        return {'covered': 0, 'with_real_str': 0, 'with_sales': 0, 'total': 0, 'pct': 0.0}
    covered = sum(1 for v in index.values() if v.get('impressions', 0) > 0 or v.get('has_sales'))
    with_real_str = sum(1 for v in index.values() if v.get('str_pct') is not None)
    with_sales = sum(1 for v in index.values() if v.get('has_sales'))
    return {
        'covered': covered,
        'with_real_str': with_real_str,
        'with_sales': with_sales,
        'total': total_candidates,
        'pct': round(covered / total_candidates * 100, 1),
    }
