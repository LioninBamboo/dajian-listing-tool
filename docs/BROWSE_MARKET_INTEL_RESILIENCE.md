# Browse market intelligence resilience (P0-3)

Collect→publish pricing used to stall when eBay Browse `item_summary/search`
returned **429** or empty `price_stats`. Behavior as of this change:

## Order of resolution

1. **Disk cache** (`cache/browse_market_intel/*.json`, TTL 6h) — `pricing_basis=BROWSE_CACHE`
2. **Browse API** with exponential backoff on HTTP 429/5xx (up to 4 attempts) — `pricing_basis=BROWSE`
3. On Browse failure: **stale cache** (ignore TTL) if present — `pricing_basis=BROWSE_CACHE`
4. **Terapeak** `analyze_competition` fallback — `pricing_basis=TERAPEAK_FALLBACK`
5. Still no median → **allow publish at SAFE_15** (15% net-on-cost via `PricingEngine.calculate_selling_price`) and set:
   - `product["pricing_basis"] = "SAFE_15_NO_MARKET"`
   - `product["market_price"] = None`

## Hard rule

Missing market price is **not** a hard stop for LIVE publish. Far-above-market
gates still require a real median; without one they must not invent a browse
ratio. SAFE_15_NO_MARKET listings are publishable and clearly flagged in logs /
product dict for later audit.

## Code

- `qwen_optimizer.QwenOptimizer.fetch_market_intelligence`
- `batch_publish.fetch_market_price` / `calculate_smart_final_price`
