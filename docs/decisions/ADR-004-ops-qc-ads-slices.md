# ADR-004: Substore QC autofix and ads as opt-in ops slices

## Status
Accepted

## Date
2026-09-14

## Context
GrovePop (outdoor furniture) and AquaRides (motors) run `scheduler_profile: ops` so they never execute the main-store full catalog (MI auto-publish, CRO price_drop, GIGA dropship). Operators now want:

1. GrovePop live QC auto-fix using the same furniture whitelist as AquaVerve.
2. Both substores to run Promoted Listings like the main store.

Turning substores onto `scheduler_profile: full` would also start MI auto-publish, CRO consume, and GIGA push. That is not acceptable.

## Decision
Keep `scheduler_profile: ops` as the substore floor. Add two default-off StoreProfile flags:

- `scheduler_enable_qc_autofix`
- `scheduler_enable_ads`

The ops scheduler and watchdog append the matching main-store *task functions* when a flag is on. Main-store `full` cadence ignores the flags.

Ads enrollment uses `scripts/store_marketing.py --levers promoted` with `EbayAdService.create_ad_safe`, not `batch_create_ads` and not `cro_promote` (the latter waits for `daily_tasks`, which ops never runs).

`create_ad_safe` falls back to Trading `GetItem` when Inventory offers are empty, so AquaRides Motors listings get the same margin guard instead of fail-open. Furniture Inventory offers still short-circuit before Trading.

Ads-enabled ops also runs the shared Mon/Thu `batch_smart_reprice` job at 09:35, before restore/enroll, matching the main-store dependency that ad restore assumes prices were just gated.

GrovePop sets `qc_profile: furniture` and both flags true. AquaRides sets only the ads flag; Motors QC stays on its dedicated Windows tasks.

## Alternatives Considered

### Switch substores to `scheduler_profile: full`
- Pros: zero new code
- Cons: MI auto-publish, CRO price_drop, GIGA dropship, finance sync all start on substores
- Rejected: violates the ops isolation red line

### Reuse `cro_promote` for substores
- Pros: identical CRO queue path
- Cons: gated on `daily_tasks` success; ops never produces that health key, so promote would skip every day
- Rejected: would look scheduled and do nothing

## Consequences
- Existing ops instances that do not flip the flags keep inventory-only cadence.
- GrovePop Content QC Windows task remains Disabled; daemon slice is the live path.
- Missing `sell.marketing` is a skip, not a scheduler failure. 2026-09-14 live probe: AquaVerve marketing is `ok`; GrovePop and AquaRides still `403 errorId 1100`. Re-consent runbook: `docs/SUBSTORE_ADS_REAUTH.md`.
- CRO diagnose / price_drop / image_refresh / fill_specifics / MI auto-publish remain main-store-only until separately opted in.
