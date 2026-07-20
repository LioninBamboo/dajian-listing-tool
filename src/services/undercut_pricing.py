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
    source_price: float
    recommended_price: float
    market_aware_price: float
    competitor_stats: Dict[str, float] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_price": self.source_price,
            "recommended_price": self.recommended_price,
            "market_aware_price": self.market_aware_price,
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
    profile: Any = None,
    competitor_stats: Optional[Dict[str, float]] = None,
) -> UndercutQuote:
    """Undercut ``source_price`` per the instance's pricing config.

    ``profile=None`` reads the active StoreProfile. Pass an explicit profile in
    tests. Competitor stats are advisory: they add reasoning/flags and an
    alternative ``market_aware_price``, but never push the recommendation above
    the source undercut.
    """
    if profile is None:
        from src.utils.store_profile import get_store_profile

        profile = get_store_profile()

    src = float(source_price)
    if src <= 0:
        raise ValueError(f"source_price must be positive, got {source_price!r}")

    pct = max(0.0, float(profile.undercut_pct))
    min_abs = max(0.0, float(profile.undercut_min_abs))

    # Undercut by the larger of pct and absolute amount, but never below ~0.
    by_pct = src * (1.0 - pct)
    by_abs = src - min_abs
    base = min(by_pct, by_abs)
    base = max(base, 0.01)

    stats = dict(competitor_stats or {})
    flags: List[str] = []
    reason_bits = [f"source ${src:.2f}", f"undercut {pct:.1%}"]
    if min_abs:
        reason_bits.append(f"min -${min_abs:.2f}")

    market_aware = base
    median = stats.get("median")
    cmin = stats.get("min")
    cmax = stats.get("max")

    if median:
        reason_bits.append(f"mkt median ${median:.2f}")
        if base > median:
            # Source looks overpriced vs the market; offer an alt under median.
            market_aware = median * (1.0 - pct)
            flags.append("source_above_market_median")
        elif base < median:
            flags.append("undercuts_market_median")
    if cmin and base < cmin:
        # We'd be the cheapest live listing — fine for undercut, but flag so the
        # operator can confirm it still clears cost (source cost handled upstream).
        flags.append("below_market_min")
    if cmax and src > cmax:
        flags.append("source_above_market_max")

    if getattr(profile, "price_ends_99", False):
        base = _to_99_at_or_below(base)
        market_aware = _to_99_at_or_below(market_aware)
    else:
        base = round(base, 2)
        market_aware = round(market_aware, 2)

    # market_aware is an alternative that is <= base by construction; keep it
    # from exceeding the plain undercut.
    market_aware = min(market_aware, base)

    return UndercutQuote(
        source_price=round(src, 2),
        recommended_price=base,
        market_aware_price=market_aware,
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
