# ADR-002: Replace Scheduled Title Optimization With Live Listing Audit

## Status
Accepted

## Date
2026-06-13

## Context

The old scheduled title optimization path was directly revising live eBay listings on a timer. Two production failures made that unsafe:

- generated titles could hallucinate unsupported claims such as foldable / assembly / material features
- multiple publish and revise paths were hard-truncating titles with raw `[:80]`, so live titles ended with broken fragments like `Inc`, `Ou`, or `wi`

That combination creates a bad failure mode: the scheduler mutates already-published listings without comparing the live eBay copy against the supplier source of truth, then customers see inaccurate titles or descriptions and return the order for mismatch.

## Decision

1. Disable scheduled title optimization by default.
2. Replace the daily `09:00` title optimization slot with a daily live listing audit at `11:30`.
3. Make `scripts/audit_fix_active_listings.py --live` the primary maintenance path for hallucination repair.
4. Require title writes to go through `src/utils/title_sanitizer.normalize_listing_title_for_ebay()` instead of raw `[:80]`.
5. When fixing live listings, use the live inventory/offer snapshot as the repair base, not just the locally stored `optimization`.
6. Reject full-inventory `--fix` runs unless `--live` is explicitly enabled.
7. Persist a report-level `source` field so every audit artifact records whether it came from `live_ebay` or `local_db_optimization`.

## Alternatives Considered

### Keep scheduled title optimization and add more prompt constraints

- Pros: less code change
- Cons: still mutates live listings without a source-of-truth comparison
- Rejected: prompt-only mitigation does not address stale local data or hard truncation in downstream publish paths

### Disable all automatic post-publish maintenance

- Pros: safest operational posture
- Cons: leaves existing hallucinations, category drift, and image-collapse issues unresolved
- Rejected: the business still needs post-publish maintenance; it just needs to be source-checked

### Audit local optimization only

- Pros: cheaper API usage
- Cons: misses drift between local DB and the real live listing
- Rejected: the incident was specifically about what customers saw on eBay, so live content must be audited

## Consequences

- The scheduler now prioritizes live audit and repair over proactive title rewriting
- Title normalization logic is centralized in `src/utils/title_sanitizer.py`
- Active listing repair paths must republish offers after inventory product changes when the live listing needs to ingest the revised title or aspects
- Full-library repair commands now fail fast if an operator tries to run `--fix` without `--live`
- Audit artifacts are operationally traceable even after logs and shell history are gone
- Operators get a safer daily maintenance loop and can target residual SKUs with `--sku-file`
