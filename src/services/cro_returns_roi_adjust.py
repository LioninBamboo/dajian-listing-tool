"""S76 — Returns 反哺 promote ROI 调整.

逻辑: promote 的 ROI = (after_revenue - before_revenue - ad_spend) / ad_spend
退货率高的 SKU, 实际净收入需扣预期退款; 调整后 ROI 可能从盈利变亏损.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

DEFAULT_RETURN_REFUND_RATIO = 1.0   # 退货=全额退款
DEFAULT_RETURN_HANDLING_FEE = 2.0   # 退货处理成本/单 (USD)
HIGH_RETURN_THRESHOLD = 0.15


def expected_loss_per_unit(price: float,
                           return_pct: float,
                           refund_ratio: float = DEFAULT_RETURN_REFUND_RATIO,
                           handling_fee: float = DEFAULT_RETURN_HANDLING_FEE,
                           ) -> float:
    return_pct = max(0.0, min(1.0, float(return_pct or 0.0)))
    return return_pct * (price * refund_ratio + handling_fee)


def adjusted_roi(roi: float,
                 price: float,
                 sold_units: float,
                 return_pct: float,
                 ad_spend: float,
                 refund_ratio: float = DEFAULT_RETURN_REFUND_RATIO,
                 handling_fee: float = DEFAULT_RETURN_HANDLING_FEE,
                 ) -> Dict[str, Any]:
    if ad_spend <= 0:
        return {
            'original_roi': roi,
            'adjusted_roi': roi,
            'expected_return_loss': 0.0,
            'note': 'no_ad_spend',
        }
    loss_per_unit = expected_loss_per_unit(price, return_pct, refund_ratio,
                                           handling_fee)
    total_loss = loss_per_unit * sold_units
    adj_roi = roi - (total_loss / ad_spend)
    return {
        'original_roi': round(roi, 4),
        'adjusted_roi': round(adj_roi, 4),
        'expected_return_loss': round(total_loss, 4),
        'expected_loss_per_unit': round(loss_per_unit, 4),
        'return_pct': return_pct,
    }


def annotate_roi_with_returns(items: Iterable[Dict[str, Any]],
                              return_lookup: Optional[
                                  Dict[str, float]] = None,
                              ) -> List[Dict[str, Any]]:
    """每条 promote ROI item ({sku, roi, price, sold_units, ad_spend})
    加 adjusted_roi 字段."""
    out = []
    for item in items:
        sku = item.get('sku')
        rp = (return_lookup or {}).get(sku, item.get('return_pct', 0.0))
        adj = adjusted_roi(
            roi=item.get('roi', 0.0),
            price=item.get('price', 0.0),
            sold_units=item.get('sold_units', 0),
            return_pct=rp,
            ad_spend=item.get('ad_spend', 0.0),
        )
        out.append({**item, **adj,
                    'flagged_high_return':
                        rp >= HIGH_RETURN_THRESHOLD})
    return out


def filter_underperforming(items: List[Dict[str, Any]],
                           min_adjusted_roi: float = 1.0,
                           ) -> List[Dict[str, Any]]:
    return [i for i in items
            if i.get('adjusted_roi', 0.0) < min_adjusted_roi]
