# CRO System

This document is the current source of truth for the CRO conversion-improvement subsystem. It covers the runtime path, scheduler cadence, active scripts, service-layer governance modules, and the invariants that must stay true when the CRO surface changes.

## Scope

The CRO subsystem starts after a listing is already live on eBay. It does four things:

1. Diagnose funnel health and enqueue actions.
2. Execute safe actions through existing repricing, image, specifics, and promotion flows.
3. Monitor the outcome and feed thresholds back into the next cycle.
4. Close the operational loop with release, audit, retention, disaster recovery, compliance, and continuous-improvement helpers.

## Runtime Flow

```text
PUBLISHED listing
    |
    v
daily_tasks.py -> run_cro_diagnose()
    |
    v
src/services/conversion_diagnoser.py
    |
    v
src/services/cro_daily_runner.py
    |
    v
logs/cro_action_queue.jsonl
    |
    +--> 10:00 scripts/batch_smart_reprice.py --from-cro-queue
    +--> 10:15 scripts/cro_image_refresh.py
    +--> 10:20 scripts/cro_fill_specifics.py
    +--> 10:25 scripts/cro_promote.py
    +--> Mon 11:00 scripts/cro_delist.py (magic-link confirm path)
    |
    v
scripts/cro_sentinel.py / scripts/cro_effect_audit.py / threshold feedback
    |
    v
src/services/cro_canary_release.py
src/services/cro_ops_runbook.py
src/services/cro_ab_finalize.py
src/services/cro_postmortem_generator.py
src/services/cro_oncall_rotation.py
src/services/cro_data_retention.py
src/services/cro_capacity_planning.py
src/services/cro_disaster_recovery.py
src/services/cro_compliance_audit.py
src/services/cro_continuous_improvement.py
src/services/cro_ops_control_plane.py
scripts/cro_ops_snapshot.py
```

## Scheduler Cadence

The current CRO-relevant schedule in scheduler_daemon.py is:

| Time | Task | Purpose |
|------|------|---------|
| `09:30` | `daily_tasks.py` | Runs the default daily workload, including `run_cro_diagnose()` |
| `09:45` | `ad_blacklist_cleanup.py` | Removes unrecoverable SKUs from ad spend |
| `09:50` | `guard_anomaly_alert.py` | Guard anomaly alerting |
| `09:55` | `task_cro_monthly_report()` | Effect-audit feedback gate |
| `10:00` | `task_cro_consume()` | Executes queued `price_drop` work through repricing |
| `10:15` | `task_cro_image_refresh()` | Executes queued image refresh work |
| `10:20` | `task_cro_fill_specifics()` | Executes queued specifics fixes |
| `10:25` | `task_cro_promote()` | Executes queued promote work; existing ads get bid uplift, missing ads use `create_ad_safe` |
| `10:30` | `task_cro_sentinel()` | Sends CRO alert signal and worsened-SKU output |
| `10:35` | `task_cro_send_offer()` | Sends margin-floored seller-initiated offers to interested buyers of low-CVR listings |
| `10:40` | `task_cro_lifecycle_detect()` | Relist-lifecycle candidate detection (local candidate rows only; no eBay mutation) |
| `10:50` | `task_cro_lifecycle_evaluate()` | Relist-lifecycle observation evaluation (local state transitions + soft-lever enqueue; never withdraw/republish) |
| Tue `10:00` | `task_smart_bid()` | Bid optimization before promote step |
| Tue `11:00` | `task_bid_rollback()` | Post-promote rollback audit |
| Mon `11:00` | `task_cro_delist_email()` | Delist candidate email with confirmation link |
| Sun `02:00` | `task_cro_learn_thresholds()` | Pending-threshold learning |
| Sun `02:30` | `task_cro_promote_thresholds()` | Threshold shadow gate to production |
| Sun `03:00` | `task_cro_ops_snapshot()` | S131-S140 ops snapshot, capacity sample, DB DR drill |

## Operator Cheat Sheet

This section is the human-readable shortcut for operators who need to understand the CRO panel or email output without reopening the service code.

### How To Read The CRO Email

The readable CRO email is rendered by `src/services/cro_email_digest.py`. The important fields mean:

| Email field | Human meaning |
|-------------|---------------|
| `队列记录` | Total queue rows processed this run. This can be larger than the number of real products because the same SKU may appear more than once. |
| `唯一 SKU` | Distinct products after merging repeated queue rows by SKU. |
| `成功` | The executor really changed something on eBay or in the downstream flow. |
| `失败` | The executor tried to act but hit an error. |
| `跳过` | The executor intentionally did nothing because a safety rule, guard rail, or precondition blocked the change. This is not the same as a failure. |
| `主要原因` | Grouped reasons for why SKUs were skipped or handled in the same way. This counts SKUs, not raw queue rows. |

The per-product table shows the thumbnail, product title, status, reason, and links. For `promote`, the most common skip reason is:

- `already at cap (...)`: the SKU already sits at its margin-aware safe bid ceiling, so CRO checked it but refused to raise the ad bid further.

SKUs skipped `already at cap` within the last 7 days enter a promote cooldown: `src/services/cro_promote_escalation.py` reads recent `logs/cro_promote_*.json` reports and the daily runner stops re-enqueueing `promote` for them, freeing the daily enqueue slots for SKUs whose bid can still move. These SKUs surface in the daily report under `promote_escalation` — the ad lever is exhausted for them, so they are the operator's candidates for non-ad levers (title/keyword rewrite, relist lifecycle). `promote_at_cap_cooldown_dropped` in the daily report counts how many promote recommendations the cooldown suppressed that day.

The escalation list feeds `scripts/cro_title_rewrite.py`, the operator-only title/keyword enrichment channel (the controlled replacement for the scheduled title optimization removed by ADR-002). It is deliberately NOT scheduled: dry-run by default, `--apply` requires `--yes`, new titles only append the SKU's own whitelisted aspect values (no AI, no invented claims), every write goes through `normalize_listing_title_for_ebay` with a live-snapshot base plus post-write verification, and a 30-day per-SKU cooldown prevents title churn.

### When Each CRO Action Appears

The diagnosis rules live in `src/services/conversion_diagnoser.py`. In operator terms:

| Action | Typical trigger | What it means operationally |
|--------|-----------------|-----------------------------|
| `promote` | Listing has no impressions for 14+ days, or has been live a long time with traffic but still no sales | eBay is not giving enough exposure, so the system tries safe ad lift first. |
| `price_drop` | Listing has impressions, but CTR or CVR is weak and price is above market median | The listing is being seen, but price is likely the main blocker. |
| `image_refresh` | Listing has impressions, CTR is weak, but price is already aligned with market | Price is probably not the problem; the thumbnail / hero image is the next lever. |
| `fill_specifics` | Listing gets clicks, but shoppers do not convert, and price is already reasonable | Buyers likely need clearer specifics or stronger description fields before they purchase. |
| `send_offer` | Same low-CVR trigger as `fill_specifics` | Interested buyers (watchers/cart adds) get a time-limited seller-initiated offer. The executor prices it against a PricingEngine-derived margin floor (net margin ≥ 5%, discount ≤ 10%, 30-day per-SKU cooldown, counter-offers disabled), so an offer can never sell below cost. |
| `delist` | Listing has 60+ days of zero impressions and zero sales | This is a dead listing candidate and must stay human-confirmed. |

### Auto-Enqueue Policy (daily runner)

`run_cro_daily()` only auto-queues a diagnosis action when both hold:

1. The action type is whitelisted by the caller (`daily_tasks.run_cro_diagnose` passes `price_drop`, `image_refresh`, `fill_specifics`, `promote`).
2. The action priority is within `enqueue_max_priority` (the daily run passes `2`).

Priority semantics from `conversion_diagnoser.py`:

| Priority | Actions | Auto-queued? |
|----------|---------|--------------|
| P1 | `price_drop` (drop), `promote` (zero-impression) | Yes |
| P2 | `image_refresh`, `fill_specifics`, `send_offer`, `title_refresh`, `promote` (30d no-sale) | Yes, if the type is whitelisted |
| P3 | `delist` | Never — human-confirmed only |
| P4 | `price_drop` reverse increase | Never |

The runner hard-caps `enqueue_max_priority` at 2, so P3/P4 can never be auto-queued regardless of caller arguments. Before 2026-07, only P1 was queued, which meant the 10:15 `image_refresh` and 10:20 `fill_specifics` executors always consumed an empty queue (the diagnoser emits both only at P2). The daily report field `queued_by_action` shows what actually entered the queue per action type.

### Why A Run May Show Only `promote`

If the daily diagnosis says most or all listings are in `no_impression`, CRO has no CTR or CVR signal to decide between price, image, or specifics fixes. In that state, the system will mostly emit:

- `promote`
- sometimes `delist` for very old zero-impression listings

It will not emit `price_drop`, `image_refresh`, or `fill_specifics` until a listing has enough exposure to prove where the funnel is leaking.

Example: in `reports/cro_daily_2026-05-10.json`, all `676` diagnosed listings fell into `no_impression`, and the action summary only contained `promote` and `delist`. That is why the later queue and email showed no pending `price_drop`, `image_refresh`, or `fill_specifics` work.

### Why Queue Stats Can Show Pending But Nothing Executable

`src/services/cro_action_queue.py` splits pending queue rows into two groups:

- `pending_executable`: rows that an executor may really consume
- `pending_control`: A/B control rows that stay pending on purpose and must not be auto-executed

If the queue says `pending = 36` but `pending_executable = 0`, that means only control rows remain. This is expected and does not mean work was missed.

### Practical Reading Order For Operators

When you open the CRO page or receive an email, read it in this order:

1. Check whether the dominant funnel stage is `no_impression`, `low_ctr`, or `low_cvr`.
2. Check `by_action_type` in the daily report or the action summary in the UI.
3. If an email says `跳过`, read the `原因` column before assuming something failed.
4. If the queue has pending rows, compare `pending_executable` against `pending_control` before deciding that execution is stuck.

## Active Files

### Core Diagnosis And Queueing

- `src/services/conversion_diagnoser.py`
- `src/services/cro_daily_runner.py`
- `src/services/cro_action_queue.py`
- `src/services/cro_thresholds.py`
- `src/services/cro_reasoning_trace.py`

### Queue Executors And Feedback

- `scripts/batch_smart_reprice.py --from-cro-queue`
- `scripts/cro_image_refresh.py`
- `scripts/cro_fill_specifics.py`
- `scripts/cro_promote.py`
- `src/services/cro_auto_executor.py`
- `src/services/cro_relist_lifecycle.py`
- `scripts/cro_relist_lifecycle.py`
- `scripts/cro_delist.py`
- `scripts/cro_sentinel.py`
- `scripts/cro_effect_audit.py`
- `scripts/cro_threshold_feedback.py`
- `scripts/cro_threshold_shadow.py`
- `scripts/cro_promote_thresholds.py`

### Ops And Governance Closure (S131-S140)

| Module | Current public surface |
|--------|------------------------|
| `src/services/cro_canary_release.py` | `init_canary`, `evaluate_health`, `promote_next_stage`, `rollback` |
| `src/services/cro_ops_runbook.py` | `RunbookRegistry`, `execute_runbook` |
| `src/services/cro_ab_finalize.py` | `evaluate_significance`, `finalize_experiment`, `archive_experiment` |
| `src/services/cro_postmortem_generator.py` | `render_postmortem` |
| `src/services/cro_oncall_rotation.py` | `current_oncall`, `next_oncall`, `schedule_for_window`, `notify_oncall` |
| `src/services/cro_data_retention.py` | `apply_retention` |
| `src/services/cro_capacity_planning.py` | `project_exhaustion`, `assess_components` |
| `src/services/cro_disaster_recovery.py` | `backup_database`, `verify_backup`, `restore_to_temp`, `run_dr_drill` |
| `src/services/cro_compliance_audit.py` | `verify_lawful_basis`, `log_access`, `query_subject_history`, `generate_dsar_export` |
| `src/services/cro_continuous_improvement.py` | `collect_signals`, `generate_suggestions`, `rank_suggestions`, `render_weekly_brief` |
| `src/services/cro_ops_control_plane.py` | `build_ops_snapshot`, `build_capacity_components_from_paths`, `render_ops_markdown`, `write_ops_snapshot` |

Legacy helper names such as `create_backup`, `restore_backup`, `log_compliance_event`, `generate_audit_report`, `search_user_activity`, `generate_improvements`, and `render_brief` still exist for compatibility, but the interfaces above are the intended operator-facing contract for S138-S140.

Production-facing S131-S140 entrypoints now go through `scripts/cro_ops_snapshot.py` and `scheduler_daemon.py --task cro_ops`. The script writes `logs/cro_ops_snapshot.json`, can write `logs/cro_weekly_improvement.md`, records capacity samples only when `--record-capacity-sample` is passed, and runs the database restore drill only when `--dr-drill` is passed. The Streamlit `🚦 CRO 状态` page reads the same snapshot.

## Invariants

- Do not bypass PricingEngine, RepricingGuard, or EbayPublisher from CRO flows.
- Queue consumers must verify live eBay state or use existing guard APIs before marking work as done.
- `promote` can create missing ads only through `EbayAdService.create_ad_safe`; unsafe, blacklisted, or campaign-less SKUs must not be marked done.
- CRO diagnose is additive; it should enqueue work, not directly mutate live listing state.
- `scripts/cro_delist.py` stays human-confirmed. Never auto-delist live listings.
- The scheduler runs relist-lifecycle detect/evaluate only. Live withdraw/republish requires the operator path (`scripts/cro_relist_lifecycle.py --precheck/--execute --apply`); `execute_approved()` is a thin orchestrator over that same two-step flow, never a bypass.
- Threshold promotion must stay shadow-gated before production promotion.
- DR helpers must preserve enough metadata to verify backup integrity before restore.
- Compliance helpers must keep lawful-basis information attached to subject-access records.
- Continuous-improvement output is advisory. It should not directly change production thresholds without an approval path.
- The ops snapshot is an observation/control-plane surface. It must not publish, delist, reprice, or promote listings directly.

## Validation

Full regression remains:

```bash
python -m pytest
```

For the S131-S140 ops/governance layer, use this focused slice first:

```bash
python -m pytest tests/test_cro_canary_release.py tests/test_cro_ops_runbook.py tests/test_cro_ab_finalize.py tests/test_cro_postmortem_generator.py tests/test_cro_oncall_rotation.py tests/test_cro_data_retention.py tests/test_cro_capacity_planning.py tests/test_cro_disaster_recovery.py tests/test_cro_compliance_audit.py tests/test_cro_continuous_improvement.py tests/test_cro_ops_control_plane.py tests/test_scheduler_cro_ops.py -q
```

## Related Docs

- `README.md` for repo-level overview
- `docs/USAGE.md` for operator workflow
- `docs/ARCHITECTURE.md` for runtime topology
- `docs/TESTING.md` for regression slices
- `docs/SCRIPT_MATRIX.md` for script ownership and active/manual split
- `docs/CRO_RELIST_REVIVE_SPEC.md` for the 30+ day no-sale lifecycle loop: revive viable stale listings, send very old zero-traffic dead links to human-confirmed final delist
- `/memories/repo/cro-system.md` for repository-scoped incremental history
