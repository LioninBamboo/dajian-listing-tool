"""Undercut pricing for collected-from-eBay listings (blind-box instance).

Blind-box products are collected from an existing eBay listing that already
carries a price. Instead of the furniture cost-plus model (PricingEngine), the
strategy here is simple: price a hair under the source listing to win the sale,
then sanity-check that against live competitor prices.

Two outputs:
  - ``recommended_price``: the source price undercut by the configured amount.
    Strictly <= source. This is the primary number.
  - ``market_aware_price``: if a competitor median is available and the plain
    undercut still sits above it (i.e. the source was itself overpriced vs the
    market), this undercuts the median instead — surfaced as an alternative,
    never silently substituted.

Competitor stats are the ``price_stats`` shape produced by the existing Browse
API research (avg/min/max/median). ``fetch_competitor_stats`` provides a
decoupled fetch, but the core ``recommend_undercut_price`` is pure and takes
stats as an argument so it is fully testable offline.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UndercutQuote:
    source_price: float             # source listing's item price
    source_shipping: float          # source listing's shipping charge
    source_total: float             # buyer's landed cost at the source (price + shipping)
    recommended_price: float        # item price we list
    recommended_shipping: float     # shipping we charge the buyer (0 when free)
    recommended_total: float        # buyer's landed cost from us (price + shipping)
    market_aware_price: float       # alt item price if source was above market median
    shipping_model: str = "free"
    competitor_stats: Dict[str, float] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_price": self.source_price,
            "source_shipping": self.source_shipping,
            "source_total": self.source_total,
            "recommended_price": self.recommended_price,
            "recommended_shipping": self.recommended_shipping,
            "recommended_total": self.recommended_total,
            "market_aware_price": self.market_aware_price,
            "shipping_model": self.shipping_model,
            "competitor_stats": self.competitor_stats,
            "flags": self.flags,
            "reasoning": self.reasoning,
        }


def _to_99_at_or_below(x: float) -> float:
    """Largest N.99 value <= x (psychological pricing, never rounds up)."""
    base = math.floor(x)
    if x + 1e-9 >= base + 0.99:
        cand = base + 0.99
    else:
        cand = base - 0.01
    return round(cand, 2) if cand > 0 else round(x, 2)


def recommend_undercut_price(
    source_price: float,
    *,
    source_shipping: float = 0.0,
    profile: Any = None,
    competitor_stats: Optional[Dict[str, float]] = None,
) -> UndercutQuote:
    """Undercut the source listing's TOTAL landed cost per the instance's config.

    The competitor's real price is item price + shipping: sellers in this category
    routinely list a low item price behind an inflated shipping fee, so undercutting
    the item price alone would mis-price badly. We undercut ``source_price +
    source_shipping`` and then split the result into the item price / shipping the
    buyer sees, per ``profile.shipping_model``:

      - free  : list price = undercut total, shipping $0 (Best Match friendly)
      - fixed : shipping = fixed_shipping_amount, item price = total - shipping

    ``profile=None`` reads the active StoreProfile. Competitor stats stay advisory.
    """
    if profile is None:
        from src.utils.store_profile import get_store_profile

        profile = get_store_profile()

    src_price = float(source_price)
    src_ship = max(0.0, float(source_shipping or 0.0))
    if src_price <= 0:
        raise ValueError(f"source_price must be positive, got {source_price!r}")
    src_total = src_price + src_ship

    pct = max(0.0, float(profile.undercut_pct))
    min_abs = max(0.0, float(profile.undercut_min_abs))

    # Undercut the TOTAL by the larger of pct and absolute amount, never below ~0.
    by_pct = src_total * (1.0 - pct)
    by_abs = src_total - min_abs
    base_total = max(min(by_pct, by_abs), 0.01)

    stats = dict(competitor_stats or {})
    flags: List[str] = []
    reason_bits = [
        f"source ${src_price:.2f}+${src_ship:.2f} ship = ${src_total:.2f}",
        f"undercut {pct:.1%}",
    ]
    if min_abs:
        reason_bits.append(f"min -${min_abs:.2f}")

    market_aware_total = base_total
    median = stats.get("median")
    cmin = stats.get("min")
    cmax = stats.get("max")

    if median:
        reason_bits.append(f"mkt median ${median:.2f}")
        if base_total > median:
            market_aware_total = median * (1.0 - pct)
            flags.append("source_above_market_median")
        elif base_total < median:
            flags.append("undercuts_market_median")
    if cmin and base_total < cmin:
        flags.append("below_market_min")
    if cmax and src_total > cmax:
        flags.append("source_above_market_max")

    # Split the undercut total into item price + shipping shown to the buyer.
    model = str(getattr(profile, "shipping_model", "free") or "free").lower()
    if model == "fixed":
        ship = max(0.0, float(getattr(profile, "fixed_shipping_amount", 0.0) or 0.0))
        ship = min(ship, base_total - 0.01)  # item price stays >= 0.01
        item = base_total - ship
        reason_bits.append(f"fixed ship ${ship:.2f}")
    else:
        model = "free"
        ship = 0.0
        item = base_total
        reason_bits.append("free ship")

    if getattr(profile, "price_ends_99", False):
        item = _to_99_at_or_below(item)
        market_aware_item = _to_99_at_or_below(max(market_aware_total - ship, 0.01))
    else:
        item = round(item, 2)
        market_aware_item = round(max(market_aware_total - ship, 0.01), 2)

    ship = round(ship, 2)
    rec_total = round(item + ship, 2)
    market_aware_item = min(market_aware_item, item)  # alt never above plain undercut

    return UndercutQuote(
        source_price=round(src_price, 2),
        source_shipping=round(src_ship, 2),
        source_total=round(src_total, 2),
        recommended_price=item,
        recommended_shipping=ship,
        recommended_total=rec_total,
        market_aware_price=market_aware_item,
        shipping_model=model,
        competitor_stats=stats,
        flags=flags,
        reasoning="; ".join(reason_bits),
    )


def fetch_competitor_stats(
    query: str,
    *,
    category_id: Optional[str] = None,
    limit: int = 30,
    environment: Optional[str] = None,
) -> Dict[str, float]:
    """Best-effort live competitor price band via eBay Browse API.

    Decoupled from qwen_optimizer.fetch_market_intelligence (which requires a
    Qwen key to construct). Returns {avg,min,max,median} or {} on any failure —
    pricing must never hard-fail on a research hiccup.
    """
    try:
        import requests
        from src.services.ebay_auth import EbayOAuthService

        oauth = EbayOAuthService(environment or os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        token = oauth.get_valid_token()
        if not token:
            return {}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        }
        params = {
            "q": query,
            "limit": limit,
            "sort": "bestMatch",
            "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW}",
        }
        if category_id:
            params["category_ids"] = category_id
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params=params,
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            return {}
        items = resp.json().get("itemSummaries", []) or []
        prices = [
            float(it.get("price", {}).get("value", 0))
            for it in items
            if it.get("price")
        ]
        prices = [p for p in prices if p > 1]
        if not prices:
            return {}
        prices.sort()
        return {
            "avg": round(sum(prices) / len(prices), 2),
            "min": round(prices[0], 2),
            "max": round(prices[-1], 2),
            "median": round(prices[len(prices) // 2], 2),
            "sample": len(prices),
        }
    except Exception:
        return {}
