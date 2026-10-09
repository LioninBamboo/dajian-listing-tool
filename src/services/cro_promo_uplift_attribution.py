"""S98 — 推广 uplift 拆分.

把推广带来的销量从总销量里拆出来:
  total_sales = organic_baseline + promoted_sales
  uplift_pct = (promoted_sales) / organic_baseline
依赖: 注入 baseline_estimate (推广前 7 天均值) + promoted_period_sales.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def split_uplift(baseline_daily: float,
                 promoted_period_sales: float,
                 promoted_days: int,
                 *,
                 ad_spend: float = 0.0,
                 unit_margin: float = 0.0,
                 ) -> Dict[str, Any]:
    """baseline_daily: 推广前每天预期 organic 销量;
    promoted_period_sales: 推广期间总销量 (含 organic + ad-driven);
    promoted_days: 推广天数."""
    if promoted_days <= 0:
        return {'error': 'invalid_promoted_days', 'uplift_units': 0,
                'uplift_pct': 0, 'organic_expected': 0}
    organic_expected = baseline_daily * promoted_days
    uplift_units = promoted_period_sales - organic_expected
    uplift_pct = (uplift_units / organic_expected) if organic_expected > 0 \
        else None
    incremental_revenue = uplift_units * unit_margin
    iroi = (incremental_revenue / ad_spend) if ad_spend > 0 else None
    return {
        'baseline_daily': round(baseline_daily, 4),
        'promoted_days': promoted_days,
        'organic_expected': round(organic_expected, 4),
        'promoted_period_sales': round(promoted_period_sales, 4),
        'uplift_units': round(uplift_units, 4),
        'uplift_pct': round(uplift_pct, 4) if uplift_pct is not None else None,
        'ad_spend': ad_spend,
        'incremental_margin_value': round(incremental_revenue, 4),
        'incremental_roi': round(iroi, 4) if iroi is not None else None,
        'is_incremental_positive': uplift_units > 0,
    }


def estimate_baseline_daily(history: Iterable[float],
                            window_days: int = 7) -> float:
    xs = [float(x or 0) for x in history][-window_days:]
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def batch_split_uplift(rows: Iterable[Dict[str, Any]],
                       *,
                       window_days: int = 7,
                       ) -> Dict[str, Any]:
    """rows: [{sku, history (recent daily list), promoted_sales,
             promoted_days, ad_spend, unit_margin}, ...]"""
    items: List[Dict[str, Any]] = []
    profitable = 0
    losers = 0
    for r in rows:
        baseline = estimate_baseline_daily(r.get('history') or [],
                                            window_days)
        out = split_uplift(
            baseline,
            float(r.get('promoted_sales', 0) or 0),
            int(r.get('promoted_days', 0) or 0),
            ad_spend=float(r.get('ad_spend', 0) or 0),
            unit_margin=float(r.get('unit_margin', 0) or 0),
        )
        out['sku'] = r.get('sku')
        items.append(out)
        iroi = out.get('incremental_roi')
        if iroi is not None and iroi >= 1.0:
            profitable += 1
        elif iroi is not None and iroi < 0.5:
            losers += 1
    return {
        'items': items,
        'count': len(items),
        'profitable_count': profitable,
        'loser_count': losers,
    }
