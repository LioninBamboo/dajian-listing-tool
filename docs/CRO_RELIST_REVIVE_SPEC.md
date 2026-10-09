# Spec: CRO Relist Revive Loop

## Objective

Build a controlled lifecycle action for stale live listings:

1. If a listing has been live for 30+ days and has no sales, it becomes a lifecycle candidate.
2. Listings with some traffic get one controlled `revive_relist` attempt: withdraw the old live listing, preserve local product data, republish the SKU as a fresh listing, and observe the new listing.
3. Very old dead links go directly to the existing human-confirmed `final_delist` flow: 60+ days, zero impressions, zero views, and no sales should not spend a revive slot.
4. After the observation window a relisted item resolves to exactly one of three outcomes: it counts as revived on a sale; if it recovered traffic but still has no sale it gets one soft-lever pass (reprice / Promoted Listings) and re-observes once; if traffic stayed dead it falls back to the existing human-confirmed `delist` flow.

The goal is to avoid wasting listing quota on dead links while still giving viable products one clean relaunch before permanent removal.

## Current Evidence

Snapshot checked on 2026-05-13:

- Active local `PUBLISHED` listings: 729
- Age >= 30 days and no sales: 527
- Age >= 30 days, no sales, zero impressions: 382
- Age >= 30 days, no sales, has traffic: 145
- Age >= 60 days and no sales: 253
- Age >= 60 days, no sales, zero impressions: 196

This volume means the feature must be rate-limited and approval-gated at first. A direct "auto delist and relist everything" rollout is too risky.

## Definitions

`sale` means any of:

- CRO snapshot `transactions > 0`
- CRO snapshot `sold_qty > 0`
- Fulfillment order data has the SKU or listing ID

`age_days` means days since `published_at`. If `published_at` is missing, use `created_at` as a fallback but mark the candidate with `age_source = created_at_fallback`.

`revive_relist` means:

- Snapshot the current DB row and live eBay state.
- Withdraw the current published offer/listing.
- Delete stale offer/inventory records only after the snapshot is saved.
- Keep product source data, images, attributes, specs, optimization, pricing, and cost data in DB.
- Set local status to `READY_TO_PUBLISH`.
- Re-run the standard `batch_publish.py --sku <SKU>` path to create a fresh listing.

`final_delist` means:

- Use the existing manual confirmation / magic-link model.
- Mark product `DELISTED`.
- Add it to MI blacklist for a TTL so it does not immediately re-enter discovery.

## Candidate Rules

### Revive Candidate

A SKU is eligible for `revive_relist` when all are true:

- Local status is `PUBLISHED`.
- Listing is active on eBay.
- `age_days >= 30`.
- No sales by the `sale` definition.
- It is not an old dead link: `age_days >= 60`, `impressions = 0`, and `views = 0` routes to `final_delist` instead.
- SKU is not already in a pending lifecycle action.
- SKU was not relisted in the last 45 days.
- SKU has not exceeded `max_relist_attempts = 1`.
- Product has at least 2 local images.
- Dajian stock is available or the existing inventory sync considers it sellable.
- The SKU is not on CRO diagnose blacklist, MI blacklist, or an operator protection list.

Recommended priority:

- `P2 revive_relist`: 30-59 days, no sales, has impressions or views.
- `P2 revive_relist`: 60+ days, no sales, has impressions or views and margin is still safe.
- `P3 revive_relist`: 30-59 days, zero impressions, no sales.
- `P3 final_delist_candidate`: 60+ days, zero impressions, no sales, or a previous revive attempt failed.

### Immediate Final Delist Candidate

Skip relist and go straight to human-confirmed delist when any are true:

- Listing is 60+ days old, has zero impressions, zero views, and no sales.
- Supplier no longer has stock or product is no longer available.
- Margin guard says the item cannot be safely priced.
- Product has fewer than 2 usable images.
- Category/aspect quality gate cannot pass.
- The SKU already had one revive attempt and failed the observation window.
- Operator manually marks "do not revive".

## Observation Evaluation

`evaluate_observations()` runs over rows in `observing` and decides each one's
next state from post-relist performance. It must reuse the same snapshot metrics
as detection (`impressions`, `views`, `transactions`, `sold_qty`) and compare
them against `metrics_before_json` (the pre-relist baseline stored at candidacy).

### Observation Window

- `observe_until = published_new_date + observe_window_days`.
- `observe_window_days` is stored on the action when it enters `observing`:
  `21` by default for `P3` rows (zero pre-relist traffic), `14` for `P2` rows
  (had pre-relist traffic).
- Rationale: eBay gives a new listing a 48-72h freshness boost and then ranks on
  performance; ~2-3 weeks is enough to see whether the fresh listing converts,
  and it matches the "treat early no-traction as an indexing problem, not a
  restart trigger" guidance.
- Do not resolve a row to a branch/terminal state before `observe_until`, except
  that a row may be marked `revived_success` early the moment a sale lands.

### Outcomes At Window Close

Resolve each `observing` row whose `observe_until` has passed into exactly one of
three outcomes:

1. `revived_success` — a `sale` (per Definitions) occurred at any point in the
   window. Unambiguous; terminal.
2. `needs_conversion_help` — no sale, but window traffic recovered above the
   configured floor and at/above the pre-relist baseline. The listing is alive
   but not converting; this is NOT a delist signal. Route the SKU to existing
   soft levers (reprice via inventory sync, Promoted Listings via
   `cro_action_queue`) and re-enter `observing` once. The soft levers must not
   withdraw or republish the listing, so its listing ID and accrued CTR history
   are preserved.
3. `fallback_delist_pending` — no sale and traffic is still dead (below the floor
   and not above baseline). Enters the existing human-confirmed delist flow.

### Thresholds

Traffic-recovery floors live in `cro_thresholds`, not hardcoded, so they flow
through the Plan 3 two-stage shadow promotion and stay governed with other CRO
thresholds. Initial proposed defaults (subject to shadow tuning):

- `observe_window_days`: 21 (P3), 14 (P2)
- `revive_traffic_floor_impressions`: 50 over the window
- `revive_traffic_floor_views`: 5 over the window
- recovery additionally requires window metrics `>=` the pre-relist baseline in
  `metrics_before_json`, so a flat-dead link cannot pass on noise alone.

### Re-Entry Cap

- `needs_conversion_help -> observing` is allowed at most once per action
  (`conversion_help_attempts <= 1`), tracked on the row.
- A second window with no sale resolves to `fallback_delist_pending`, never an
  unbounded reprice/promote loop.

## State Machine

Use a SQLite table, not only the JSONL CRO queue, because relist is multi-step and must be recoverable.

Table: `cro_listing_lifecycle_actions`

Required fields:

- `id`
- `sku`
- `action_type`: `revive_relist` or `final_delist`
- `status`
- `priority`
- `reason`
- `old_listing_id`
- `new_listing_id`
- `old_offer_id`
- `new_offer_id`
- `attempt_no`
- `metrics_before_json`
- `db_snapshot_json`
- `live_snapshot_json`
- `created_at`
- `approved_at`
- `started_at`
- `finished_at`
- `observe_until`
- `observe_window_days`
- `conversion_help_attempts`
- `error`
- `source`

Statuses:

- `candidate`: detected, not approved.
- `approved`: operator approved or policy allows auto-run.
- `prechecked`: inventory, sales, images, price, and active listing were rechecked.
- `old_withdrawn`: old live listing was withdrawn.
- `ready_to_publish`: DB is ready for standard publish.
- `published_new`: fresh listing exists.
- `observing`: waiting for post-relist performance window.
- `revived_success`: relisted listing got a sale within the observation window.
- `needs_conversion_help`: relisted listing recovered traffic but not sales; routed to soft levers (reprice / Promoted Listings) for one more observation pass.
- `fallback_delist_pending`: revive failed; waiting for final-delist confirmation.
- `final_delisted`: final delist completed.
- `skipped`: guard rule blocked execution.
- `failed`: execution failed and needs operator review.

Allowed transitions:

```text
candidate -> approved -> prechecked -> old_withdrawn -> ready_to_publish
ready_to_publish -> published_new -> observing
observing -> revived_success
observing -> needs_conversion_help -> observing   (at most once)
observing -> fallback_delist_pending -> final_delisted
candidate/approved/prechecked -> skipped
any execution status -> failed
```

## Execution Contract

New service: `src/services/cro_relist_lifecycle.py`

Public functions:

```python
def ensure_schema(db_path: Path | None = None) -> None: ...

def detect_candidates(
    snapshot_date: str | None = None,
    min_age_days: int = 30,
    limit: int = 200,
    db_path: Path | None = None,
) -> dict: ...

def approve_actions(
    action_ids: list[int],
    operator: str = "system",
    db_path: Path | None = None,
) -> dict: ...

def execute_approved(
    limit: int = 10,
    apply_changes: bool = False,
    db_path: Path | None = None,
) -> dict: ...

def evaluate_observations(
    snapshot_date: str | None = None,
    db_path: Path | None = None,
) -> dict: ...
```

Execution must reuse existing eBay and publish surfaces:

- eBay withdraw/delete: `RealEbayClient.delist_sku()` or a narrower wrapper that snapshots before delete.
- Publish: `batch_publish.py --sku <SKU>` or its internal `publish_single_product()` path.
- Final delist email: extend `scripts/cro_delist.py` to include `fallback_delist_pending`.

Do not publish or delist directly from the Streamlit table.

## UI Contract

Add a CRO sub-panel: `生命周期动作`.

Display columns:

- priority
- SKU
- old listing
- age days
- impressions
- views
- sales
- current action
- reason
- attempt count
- status
- execution mode

Buttons:

- `生成候选`: dry-run detection, no eBay changes.
- `批准重刊`: moves selected `candidate` rows to `approved`.
- `执行已批准`: runs approved actions with rate limit.
- `生成最终下架确认`: for `fallback_delist_pending`, creates magic-link delist email.

Default execution mode:

- `revive_relist` starts as manual approval.
- Auto-run can be enabled later only for P3 zero-impression/no-sale rows after the first stable week.
- `final_delist` always stays human-confirmed.

## Scheduler Contract

Recommended cadence:

- `10:40` daily: detect candidates after CRO diagnosis and safe actions.
- `10:45` daily: execute at most 10 approved `revive_relist` actions.
- `11:05` Monday: include fallback delist candidates in the existing delist email.

Recovery rules:

- Use the existing scheduler lock pattern.
- Do not execute if `daily_tasks` has not completed for the day.
- Do not execute if another publish/delist task is already running.
- Resume from SQLite status if the machine sleeps or the process exits.

## Safety Rules

Always:

- Snapshot old listing state before withdraw/delete.
- Recheck sales immediately before execution.
- Recheck eBay active listing status immediately before execution.
- Refuse if SKU has sales after candidate creation.
- Refuse if product lacks images, price, stock, or required publishing data.
- Rate-limit relist execution.
- Keep final delist human-confirmed.
- Write an audit row for every attempted state transition.

Ask first:

- Running `revive_relist` automatically without approval.
- Increasing `max_relist_attempts` above 1.
- Reusing a different SKU for the fresh listing.

Never:

- Delete local product data during revive.
- Reset `optimization`, `cost_breakdown`, `images`, `attributes`, or `specs` for revive.
- Mark a SKU as `DELISTED` because a relist publish failed.
- Queue the same SKU for `revive_relist` and `final_delist` at the same time.

## Implementation Plan

### Phase 1: Foundation

1. Add `cro_listing_lifecycle_actions` schema and transition helpers.
2. Add candidate detector using `collected_products` + `cro_snapshots`.
3. Route old zero-traffic dead links directly to `final_delist` candidates for the magic-link confirmation flow.
4. Add focused tests for age/sales eligibility, direct final-delist routing, and duplicate prevention.

Verification:

```bash
.venv\Scripts\python.exe -m pytest tests/test_cro_relist_lifecycle.py -q
```

### Phase 2: Dry-Run CLI And Reports

1. Add `scripts/cro_relist_lifecycle.py --detect --dry-run --out ...`.
2. Add `scripts/cro_relist_lifecycle.py --delist-links --dry-run --out ... --html ...`.
3. Require explicit `--dry-run` or `--apply`; dry-run operates on a temporary SQLite copy.
4. Add email/report preview without eBay mutation. Applying delist links only writes `cro_delist_pending` tokens; it does not delist.
5. Streamlit read-only panel remains a later UI increment.

Verification:

```bash
.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --detect --dry-run --limit 50 --out reports\cro_relist_lifecycle_detect_preview.json
.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --delist-links --dry-run --limit 50 --out reports\cro_relist_final_delist_links_preview.json --html reports\cro_relist_final_delist_links_preview.html
.venv\Scripts\python.exe -m pytest tests/test_cro_relist_lifecycle_cli.py tests/test_cro_delist.py tests/test_cro_relist_lifecycle.py -q
```

### Phase 3: Approved Execution

1. Implement `execute_approved(apply_changes=True, limit=...)`.
2. Add preflight checks for sales, stock, images, active listing, and publish readiness.
3. Withdraw old listing, set DB to `READY_TO_PUBLISH`, call standard publish path, and store `new_listing_id`.
4. Add failure recovery so partial states do not duplicate listings.

Verification:

```bash
.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --execute --limit 1
```

Start with one manually selected SKU.

### Phase 4: Observation And Fallback

Implement per the Observation Evaluation section above.

1. Implement `evaluate_observations()`: join `observing` rows to the latest
   snapshot, compute window metrics vs `metrics_before_json`, and resolve each
   row whose `observe_until` has passed.
2. Mark `revived_success` on a sale (allowed early, the moment a sale lands).
3. Mark `needs_conversion_help` on traffic recovery without a sale; trigger the
   reprice / Promoted Listings soft levers (`cro_action_queue`) and re-enter
   `observing` once (`conversion_help_attempts <= 1`). Do not withdraw/republish.
4. Mark `fallback_delist_pending` when traffic stays dead; extend
   `scripts/cro_delist.py` to include these in the magic-link email.
5. Read floors and `observe_window_days` from `cro_thresholds` so they promote
   through the Plan 3 two-stage shadow gate.

Verification:

```bash
.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --evaluate
.venv\Scripts\python.exe -m pytest tests/test_cro_delist.py tests/test_cro_relist_lifecycle.py -q
```

## Success Criteria

- Candidate detection can reproduce the expected scale without mutating eBay.
- A candidate cannot be duplicated while pending or observing.
- A SKU with any sales is never relisted or final-delisted by this flow.
- A revive action preserves local product data and creates a new listing ID.
- A failed revive does not become final delist automatically; it enters confirmation.
- Scheduler recovery can resume without duplicate email or duplicate relist.

## Open Questions

1. Resolved 2026-06-28: `revived_success` requires a `sale`. Traffic recovery without a sale routes to `needs_conversion_help` (soft levers + one more observation pass), which is neither success nor delist. See Observation Evaluation.
2. Resolved 2026-06-28: relist immediately; do not promote before relist. Promotion spend is reserved for the post-relist `needs_conversion_help` fork, so it only targets listings that have proven they can recover traffic.
3. Resolved 2026-05-13: 60+ days with zero impressions, zero views, and no sales routes directly to `final_delist` candidate; 30-59 day rows remain revive candidates.
4. Who should be recorded as the operator for approvals in the Streamlit app?
