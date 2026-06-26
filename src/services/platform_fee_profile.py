"""跨平台 Fee Profile 抽象 (P11, 2026-05).

把 `PricingEngine` 里钉死的 eBay 数 (EBAY_FEE_RATE / FIXED_FEE / STORE_DISCOUNT_RATE)
抽象成 PlatformFeeProfile, 为 Walmart / Amazon / TikTok Shop 留口.

当前用法:
    profile = get_profile('ebay')           # 默认 = 历史 PricingEngine 行为
    denom = profile.discount_denom(ad_rate=0.05)
    floor = profile.absolute_floor(cost=20)

回归保证 (`test_platform_fee_profile.py`):
    `EbayFeeProfile` 计算结果与 `PricingEngine._discount_denom` /
    `PricingEngine.absolute_floor_price` 在所有 ad_rate 下 100% 一致 (Decimal exact).

后续接入新平台只需 register_profile('walmart', WalmartFeeProfile()).
PricingEngine 自身保持不变 — 任何要支持多平台的新代码用 profile, 老代码继续用 PricingEngine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional


_Q2 = Decimal("0.01")


@dataclass(frozen=True)
class PlatformFeeProfile:
    """单一平台的费率画像. Decimal 精确.

    fee_rate           平台 FVF (eBay 13.25% / Amazon 15% / Walmart 6-15%)
    default_ad_rate    平台默认广告率 (用于 ad_rate=None 调用)
    fixed_fee          每单固定费 (eBay $0.30; Amazon 通常 0)
    store_discount_rate 长期店铺折扣 (买家折扣)
    """
    name: str
    fee_rate: Decimal
    default_ad_rate: Decimal
    fixed_fee: Decimal
    store_discount_rate: Decimal = field(default=Decimal("0"))

    def _resolve_ad(self, ad_rate: Optional[float]) -> Decimal:
        if ad_rate is None:
            return self.default_ad_rate
        return Decimal(str(ad_rate))

    def discount_denom(self, ad_rate: Optional[float] = None) -> Decimal:
        ad = self._resolve_ad(ad_rate)
        if ad < 0 or ad >= Decimal("1") - self.fee_rate:
            raise ValueError(
                f"ad_rate must be in [0, {1 - float(self.fee_rate)}), got {ad_rate}"
            )
        return (Decimal("1") - self.store_discount_rate) * (
            Decimal("1") - self.fee_rate - ad
        )

    def absolute_floor(self, cost, ad_rate: Optional[float] = None) -> Decimal:
        """死线: net == cost. listing_price = (cost + fixed_fee) / denom."""
        cost_d = Decimal(str(cost))
        denom = self.discount_denom(ad_rate)
        return ((cost_d + self.fixed_fee) / denom).quantize(_Q2, rounding=ROUND_HALF_UP)

    def safe_floor(self, cost, safety_margin: float = 0.05,
                    ad_rate: Optional[float] = None) -> Decimal:
        """安全底: net == cost × (1+safety_margin)."""
        cost_d = Decimal(str(cost)) * (Decimal("1") + Decimal(str(safety_margin)))
        denom = self.discount_denom(ad_rate)
        return ((cost_d + self.fixed_fee) / denom).quantize(_Q2, rounding=ROUND_HALF_UP)

    def required_ad_rate(self, price, cost, safety_margin: float = 0.05) -> Decimal:
        """反算: 给定 price/cost, 在保 safety_margin 利润下能支撑的最大广告率."""
        price_d = Decimal(str(price))
        target_net = Decimal(str(cost)) * (Decimal("1") + Decimal(str(safety_margin)))
        # net = price × (1-discount) × (1-fvf-ad) - fixed_fee >= target_net
        # → (1-fvf-ad) >= (target_net + fixed) / [price × (1-discount)]
        # → ad <= 1 - fvf - (target_net + fixed) / [price × (1-discount)]
        denom_no_ad_factor = price_d * (Decimal("1") - self.store_discount_rate)
        if denom_no_ad_factor <= 0:
            return Decimal("-1")
        max_ad = Decimal("1") - self.fee_rate - (target_net + self.fixed_fee) / denom_no_ad_factor
        return max_ad


# ─── 内置 Profile (与 PricingEngine 数字 100% 一致) ─────────
EBAY_PROFILE = PlatformFeeProfile(
    name='ebay',
    fee_rate=Decimal("0.1325"),
    default_ad_rate=Decimal("0.05"),
    fixed_fee=Decimal("0.30"),
    store_discount_rate=Decimal("0.05"),
)

# 占位 — 实际接入时再校准
WALMART_PROFILE = PlatformFeeProfile(
    name='walmart',
    fee_rate=Decimal("0.15"),         # 通常 6-15% 按品类
    default_ad_rate=Decimal("0"),     # walmart sponsored 单独计费, 默认不强制
    fixed_fee=Decimal("0"),
    store_discount_rate=Decimal("0"),
)

# Walmart 按品类 referral fee — 选用最常见的 Home & Garden / Electronics
# 来源: Walmart Marketplace Seller Help (2025-Q4 published rates)
WALMART_FEE_BY_CATEGORY: Dict[str, Decimal] = {
    'home_garden': Decimal("0.15"),
    'electronics': Decimal("0.08"),
    'apparel': Decimal("0.15"),
    'beauty': Decimal("0.08"),       # ≤$10: 8%, >$10: 15%
    'sports_outdoors': Decimal("0.15"),
    'tools': Decimal("0.15"),
    'office': Decimal("0.15"),
    'baby': Decimal("0.08"),
    'jewelry': Decimal("0.20"),
    'default': Decimal("0.15"),
}

AMAZON_PROFILE = PlatformFeeProfile(
    name='amazon',
    fee_rate=Decimal("0.15"),
    default_ad_rate=Decimal("0"),
    fixed_fee=Decimal("0"),
    store_discount_rate=Decimal("0"),
)


_REGISTRY: Dict[str, PlatformFeeProfile] = {
    'ebay': EBAY_PROFILE,
    'walmart': WALMART_PROFILE,
    'amazon': AMAZON_PROFILE,
}


def walmart_profile_for_category(category: str) -> PlatformFeeProfile:
    """根据 Walmart 品类返回精确 referral fee 的 profile."""
    rate = WALMART_FEE_BY_CATEGORY.get(
        (category or 'default').lower(), WALMART_FEE_BY_CATEGORY['default']
    )
    return PlatformFeeProfile(
        name=f'walmart_{category}',
        fee_rate=rate,
        default_ad_rate=Decimal("0"),
        fixed_fee=Decimal("0"),
        store_discount_rate=Decimal("0"),
    )


def assert_safe_price(price, cost, *, profile_name: str = 'ebay',
                       safety_margin: float = 0.05,
                       ad_rate: Optional[float] = None) -> None:
    """跨平台守门员: price 必须 >= profile.safe_floor(cost, safety_margin, ad_rate).

    Raises:
        ValueError: 价格不安全 (附 profile + 死线 + 实际差额).
    """
    profile = get_profile(profile_name)
    floor = profile.safe_floor(cost, safety_margin=safety_margin, ad_rate=ad_rate)
    price_d = Decimal(str(price))
    if price_d < floor:
        raise ValueError(
            f"[{profile.name}] price ${price_d} < safe_floor ${floor} "
            f"(cost=${cost}, safety={safety_margin*100:.0f}%, "
            f"ad={'auto' if ad_rate is None else f'{ad_rate*100:.1f}%'}); "
            f"差额 ${(floor - price_d).quantize(_Q2)}"
        )


def get_profile(name: str) -> PlatformFeeProfile:
    key = (name or 'ebay').lower()
    if key not in _REGISTRY:
        raise KeyError(f"unknown platform profile: {name} (registered: {list(_REGISTRY)})")
    return _REGISTRY[key]


def register_profile(name: str, profile: PlatformFeeProfile) -> None:
    _REGISTRY[name.lower()] = profile


def list_profiles() -> Dict[str, PlatformFeeProfile]:
    return dict(_REGISTRY)
