"""Offer 保本定价 — Seller-Initiated Offer 的地板价守门.

原则: 让利金额不由执行器拍脑袋, 由 PricingEngine 的同一套费率常量推导.
净利模型与 competition_monitor.calc_net_margin 完全一致:

    实收 = 价 × (1 - 店铺折扣)
    净利 = 实收 × (1 - eBay费率 - 广告费率) - 固定费 - 总成本
    净利率 = 净利 / 价  ≥  MIN_NET_MARGIN

解出保本地板价:

    floor = (固定费 + 总成本) / ((1-店铺折扣)(1-eBay费率-广告费率) - MIN_NET_MARGIN)

offer 价 = 现价 × (1 - 折扣%), 折扣封顶 MAX_DISCOUNT_PCT;
offer < floor → 抬到 floor; floor ≥ 现价 → 无让利空间, 不发 (skip).
"""
from __future__ import annotations

import math
from typing import Any, Dict

from src.services.pricing_engine import PricingEngine

MIN_NET_MARGIN = 0.05     # 让利后仍须保住的最低净利率
MAX_DISCOUNT_PCT = 10.0   # 单次 offer 折扣硬上限
DEFAULT_DISCOUNT_PCT = 5.0
MIN_MEANINGFUL_DISCOUNT_PCT = 2.0  # 低于此折扣的 offer 对买家无感知, 不值得发


def _net_revenue_ratio() -> float:
    """每 1 美元标价中扣除店铺折扣/eBay费/广告费后落袋的比例."""
    store = float(PricingEngine.STORE_DISCOUNT_RATE)
    fee = float(PricingEngine.EBAY_FEE_RATE)
    ad = float(PricingEngine.AD_RATE)
    return (1 - store) * (1 - fee - ad)


def floor_price(cost: float, min_margin: float = MIN_NET_MARGIN) -> float:
    """净利率 ≥ min_margin 的最低标价. cost 无效时返回 0 (调用方须先校验)."""
    if cost is None or cost <= 0:
        return 0.0
    denom = _net_revenue_ratio() - min_margin
    if denom <= 0:
        return float('inf')
    fixed = float(PricingEngine.FIXED_FEE)
    # 向上取整到分: 四舍五入可能把地板压到最低净利率之下, 保本闸只能上不能下
    return math.ceil((fixed + cost) / denom * 100) / 100


def compute_offer(price: float, cost: float,
                  discount_pct: float = DEFAULT_DISCOUNT_PCT,
                  ) -> Dict[str, Any]:
    """计算保本 offer 价.

    返回 {safe, offer_price, floor, discount_pct, reason}.
    safe=False 时执行器必须 skip, 绝不发 offer.
    """
    if not price or price <= 0 or not cost or cost <= 0:
        return {'safe': False, 'offer_price': None, 'floor': None,
                'discount_pct': 0.0, 'reason': 'missing economics (price/cost)'}
    pct = min(float(discount_pct or DEFAULT_DISCOUNT_PCT), MAX_DISCOUNT_PCT)
    pct = max(pct, 0.0)
    floor = floor_price(cost)
    if floor >= price:
        return {'safe': False, 'offer_price': None, 'floor': floor,
                'discount_pct': 0.0,
                'reason': f'no discount room (floor {floor} >= price {price})'}
    offer = round(price * (1 - pct / 100), 2)
    if offer < floor:
        offer = floor
        pct = round((1 - offer / price) * 100, 2)
    if pct < MIN_MEANINGFUL_DISCOUNT_PCT:
        return {'safe': False, 'offer_price': None, 'floor': floor,
                'discount_pct': pct,
                'reason': f'discount too small to matter ({pct}%)'}
    return {'safe': True, 'offer_price': offer, 'floor': floor,
            'discount_pct': pct, 'reason': 'ok'}
