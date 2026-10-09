"""
Unified scoring for the Market Intelligence module.

History: opportunity / demand / competition scores were defined in
three different places (intelligence_service, trend_discovery, and
discover_hot_keywords) with conflicting weights and thresholds. This
module centralizes them so every UI tab and report uses the same math.

Inputs are intentionally explicit (no Dict-of-anything) so callers
must surface their data quality, and so scoring can be unit-tested.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

CompetitionLevel = Literal["low", "medium", "high", "very_high", "unknown"]


# ---------------------------------------------------------------------------
# Competition
# ---------------------------------------------------------------------------

# Active-listing thresholds. Use the eBay search `total` field (real
# population), NOT the per-page `limit`. If only sample size is known,
# pass it via classify_competition(active_total=None, sample_size=...)
# and the caller is expected to flag the result as approximate.
_COMP_THRESHOLDS = (
    (200, "low", 25),        # < 200 active listings
    (1000, "medium", 50),    # 200 - 999
    (5000, "high", 75),      # 1000 - 4999
    (float("inf"), "very_high", 95),
)


def classify_competition(active_total: Optional[int], sample_size: int = 0) -> tuple[CompetitionLevel, int]:
    """
    Returns (level, score 0-100). Higher score = MORE competition.

    Prefer `active_total` (eBay's reported total). Falls back to
    `sample_size` only when total is missing, scaled up so the
    legacy "≥ 50 results out of limit=50" still maps to "high".
    """
    n = active_total if active_total and active_total > 0 else None
    if n is None:
        if sample_size <= 0:
            return ("unknown", 0)
        # Heuristic upscale: if all returned items were the cap, we
        # likely saw a saturated query. Multiply by 20 as a coarse
        # proxy so the old behavior (sample-only) stays directionally
        # correct but visibly weaker than a real-total measurement.
        n = sample_size * 20

    for upper, level, score in _COMP_THRESHOLDS:
        if n < upper:
            return (level, score)  # type: ignore[return-value]
    return ("very_high", 95)


# ---------------------------------------------------------------------------
# Opportunity score
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringInputs:
    """All numeric signals needed to score one keyword/product opportunity."""
    margin_rate: float                   # 0.0-1.0 (smart_price - cost) / cost
    competition_level: CompetitionLevel  # from classify_competition()
    price_spread: float = 0.0            # std/mean of filtered price set
    real_str: Optional[float] = None     # 0-100, only when sold-data available
    estimated_str: Optional[float] = None  # 0-100, fallback heuristic
    active_total: Optional[int] = None   # for context; not directly weighted


# Weight allocation:
#   margin     40
#   STR        30  (real preferred; estimated counts at 0.6x)
#   competition 20 (penalty)
#   spread     10  (more spread => more pricing room)
def opportunity_score(inputs: ScoringInputs) -> int:
    """
    Single source of truth for the 0-100 opportunity score.
    """
    score = 0.0

    # Margin (0..40)
    m = max(0.0, inputs.margin_rate)
    if m >= 0.35:
        score += 40
    elif m >= 0.25:
        score += 32
    elif m >= 0.15:
        score += 22
    elif m >= 0.10:
        score += 12
    else:
        score += max(0.0, m * 100)  # tiny credit for any positive margin

    # STR (0..30)
    if inputs.real_str is not None and inputs.real_str > 0:
        score += min(30.0, inputs.real_str * 1.2)
    elif inputs.estimated_str is not None and inputs.estimated_str > 0:
        # Discount the estimate so it never beats real data.
        score += min(18.0, inputs.estimated_str * 0.6)

    # Competition (0..20, penalty model)
    comp_credit = {
        "low": 20,
        "medium": 12,
        "high": 4,
        "very_high": 0,
        "unknown": 8,
    }.get(inputs.competition_level, 8)
    score += comp_credit

    # Price spread (0..10) - more dispersion = more room to position
    s = max(0.0, inputs.price_spread)
    if s >= 0.5:
        score += 10
    elif s >= 0.3:
        score += 6
    elif s >= 0.15:
        score += 3

    return max(0, min(100, int(round(score))))


# ---------------------------------------------------------------------------
# Recommendation text
# ---------------------------------------------------------------------------

def recommendation_text(score: int, margin_rate: float, competition_level: str) -> str:
    """Short Chinese recommendation, used by both UI tabs."""
    pct = margin_rate * 100
    if score >= 80:
        return f"🔥 强烈推荐 - 利润率{pct:.0f}%, 竞争{competition_level}"
    if score >= 65:
        return f"✅ 推荐刊登 - 利润率{pct:.0f}%"
    if score >= 50:
        return f"⚠️ 可以考虑 - 利润率{pct:.0f}%, 需差异化运营"
    return "❌ 暂不推荐 - 利润空间或市场竞争需优化"


# ---------------------------------------------------------------------------
# Demand signal (F2-pivot: STR proxy from Browse-only data)
#
# Marketplace Insights API access was denied by eBay, so we cannot get
# real Sell-Through Rate. Instead we expose a transparent 0-100 demand
# signal derived from data we DO have via Browse API:
#   - active_total      : how big is the live market
#   - sample_size       : how many real listings we observed (signal density)
#   - price_spread      : std/mean of filtered prices (room to position)
#   - estimated_str     : optional heuristic from HIGH_STR_KEYWORDS
#
# This is intentionally NOT called "STR" anywhere — it is a composite
# demand-side proxy. Always render with the label so users don't confuse
# it with sold-transaction data.
# ---------------------------------------------------------------------------

DemandLevel = Literal["weak", "moderate", "strong", "very_strong", "unknown"]


@dataclass(frozen=True)
class DemandSignal:
    score: int          # 0-100
    level: DemandLevel
    label: str          # short Chinese label, e.g. "需求强 (估算)"
    components: dict    # transparency: each sub-score


def compute_demand_signal(
    active_total: Optional[int],
    sample_size: int,
    price_spread: float,
    estimated_str: Optional[float] = None,
) -> DemandSignal:
    """Compute a Browse-only demand-side proxy in [0, 100].

    Weights:
        market_size  35   (active_total — bigger pool ≈ more buyers)
        density      20   (sample_size returned by Browse — popularity signal)
        spread       15   (price dispersion — pricing room)
        heuristic    30   (estimated_str if provided)
    """
    if (active_total is None or active_total <= 0) and sample_size <= 0:
        return DemandSignal(0, "unknown", "需求未知", {})

    # Market size (0..35) — log-ish thresholds
    n = active_total if (active_total and active_total > 0) else sample_size * 20
    if n >= 5000:
        size_score = 35
    elif n >= 1000:
        size_score = 28
    elif n >= 300:
        size_score = 20
    elif n >= 80:
        size_score = 12
    elif n > 0:
        size_score = 6
    else:
        size_score = 0

    # Sample density (0..20) — Browse returned items
    if sample_size >= 80:
        density_score = 20
    elif sample_size >= 40:
        density_score = 14
    elif sample_size >= 15:
        density_score = 8
    elif sample_size > 0:
        density_score = 3
    else:
        density_score = 0

    # Price spread (0..15)
    s = max(0.0, price_spread)
    if s >= 0.4:
        spread_score = 15
    elif s >= 0.2:
        spread_score = 9
    elif s > 0:
        spread_score = 4
    else:
        spread_score = 0

    # Heuristic STR contribution (0..30)
    est = estimated_str if (estimated_str and estimated_str > 0) else 0.0
    heur_score = min(30.0, est * 1.2)

    total = int(round(size_score + density_score + spread_score + heur_score))
    total = max(0, min(100, total))

    if total >= 75:
        level: DemandLevel = "very_strong"
        label = "需求很强 (估算)"
    elif total >= 55:
        level = "strong"
        label = "需求强 (估算)"
    elif total >= 35:
        level = "moderate"
        label = "需求中等 (估算)"
    else:
        level = "weak"
        label = "需求弱 (估算)"

    return DemandSignal(
        score=total,
        level=level,
        label=label,
        components={
            "market_size": size_score,
            "density": density_score,
            "spread": spread_score,
            "heuristic": int(round(heur_score)),
        },
    )
