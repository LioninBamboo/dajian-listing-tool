# Agent Guide

This file is for future coding agents working in this repository. It is intentionally concise. Read it together with `README.md`, `docs/USAGE.md`, `docs/CRO_SYSTEM.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`, and `skill.md` before making non-trivial changes.

## Core Rules

- Current long-running scheduler entrypoint is `scheduler_daemon.py`.
- Current daily workload entrypoint is `daily_tasks.py`.
- `daily_tasks.py` also runs `run_cro_diagnose()`; CRO execution is then consumed later by scheduler jobs rather than inline in the same step.
- Generated listing correctness is centralized in `src/utils/listing_quality_gate.py`; do not fork product-family, item-specific, image, or measurement blockers in one-off scripts.
- Do not publish READY drafts before running `scripts/audit_fix_ready_drafts.py` and checking `logs/ready_draft_audit_*.json`.
- Treat any `quality_gate` unresolved entry in `logs/ready_draft_audit_*.json` as a publish blocker.
- The scheduled `11:30` live listing audit is detect-only: `scripts/audit_fix_active_listings.py --live --email` reports and emails, but does not auto-fix the full live corpus.
- In `logs/listing_audit_fix_*.json`, `total_with_issues` counts unique listings with at least one issue. One listing row can carry multiple nested `issues[]`.
- When a live audit shows a large count, inspect the nested `issues[].type` breakdown before assuming duplication. Historical `hallucinated_foldable` residue can dominate the total.
- Do not treat fallback values like `See Description` as valid `Item Length`, `Item Width`, `Item Height`, or `Item Weight`.
- `Item Weight` is optional when no trustworthy product weight exists, but if present it must be real and non-placeholder.
- Shipping weight and package dimensions belong in `packageWeightAndSize`, not in fabricated item specifics.
- Do not overwrite an existing draft category unless the replacement category is clearly plausible for the product family.
- Coffee tables, benches, ottomans, patio sets, dining chairs, and sofas have explicit product-family rules. Update `listing_quality_gate.py`, `ebay_category_matcher.py`, and tests together when changing them.
- Furniture drafts must not carry shoe, sport, or pet item specifics.
- When updating JSON columns on SQLAlchemy models, call `flag_modified()` before commit.
- OAuth tokens live in `ebay_tokens.db`, not in environment variables.
- Do not strip GigaB2B signed image query parameters `x-cc`, `x-cu`, `x-ct`, `x-cs` during publish or inventory revise.
- If local DB has multiple source images but live eBay has only one, treat it as a publish or revise image issue, not a collection issue.
- Local price state must only be written after the live eBay offer price verifies successfully.
- Root underscore-prefixed diagnostics belong under `tools/local_diagnostics/`, not the repo root.
- Runtime caches now write to `cache/`; legacy root cache filenames are read-compatible fallback only.
- If you change `src/services/cro_*.py` or `scripts/cro_*.py`, update `docs/CRO_SYSTEM.md` and `/memories/repo/cro-system.md` together.
- Root cleanup starts with `python tools/root_structure_audit.py`; do not move root files before confirming callers and compatibility paths.

## CRO-Specific Rules

- `scheduler_daemon.py` owns the CRO cadence around `09:45-10:30`; `cro_promote` runs daily at `10:25`, while Sunday threshold jobs and Monday delist confirmations stay weekly/manual. Do not document an older shortened schedule.
- Keep CRO diagnose, queueing, execution, and ops/governance modules conceptually separate; do not fold them into one script without a clear reason.
- S131-S140 production observation flows through `src/services/cro_ops_control_plane.py`, `scripts/cro_ops_snapshot.py`, `scheduler_daemon.py --task cro_ops`, and the Streamlit `🚦 CRO 状态` page.
- `src/services/cro_disaster_recovery.py` public DR interface is `backup_database` / `verify_backup` / `restore_to_temp` / `run_dr_drill`; compatibility helpers may exist but should not replace that contract in docs.
- `src/services/cro_compliance_audit.py` public compliance interface is `verify_lawful_basis` / `log_access` / `query_subject_history` / `generate_dsar_export`.
- `src/services/cro_continuous_improvement.py` public advisory interface is `collect_signals` / `generate_suggestions` / `rank_suggestions` / `render_weekly_brief`.
- Do not invent new root-level CRO helper scripts when the feature belongs in `scripts/` or `src/services/`.
- The CRO ops snapshot is observation/advisory only. It must not directly publish, delist, reprice, or promote listings.

## MI-Specific Rules

- MI is a first-class subsystem and currently runs inside the default `python daily_tasks.py` flow through `run_mi_snapshot()`.
- `run_mi_snapshot()` and the Streamlit MI manual discover action now auto-prepare discovered `PENDING` / `COLLECTED` opportunities into local `READY` drafts, but do not auto-publish them.
- There is no standalone `--mi-only` CLI switch today. Do not document or rely on one unless you add it in code and tests.
- `scheduler_daemon.py` runs `task_mi_self_check()` at `09:35`; it must treat missing snapshot, missing digest, missing or empty trend history, unreadable trend history, and stale trend state as failures.
- MI diagnostic entrypoint is `python scripts/mi_diagnose.py` or `python scripts/mi_diagnose.py --json`.
- MI artifacts live in `reports/mi_opportunities_*.json`, `reports/mi_digest_*.html`, `reports/mi_long_window_history.json`, `reports/mi_alerts_state.json`, and `reports/mi_blacklist.json`.
- MI alert emails and MI daily digest emails must remain Chinese and include thumbnails.
- `mi_categories.json` is the current category-rule source. Keep the loader and tests aligned if you change its structure.
- Dashboard READY drafts use an MI-origin marker derived from structured optimization metadata or the durable MI log marker; do not remove both without replacing the operator signal.

## Safe READY-Draft Workflow

1. Run `python scripts/audit_fix_ready_drafts.py`.
2. Inspect the latest `logs/ready_draft_audit_*.json`.
3. Only move forward with drafts that have no unresolved measurement, category, image, or `quality_gate` blockers.
4. Run `python batch_publish.py --dry-run` before any live publish.
5. Publish only the drafts that pass dry-run validation.

Current publish blockers:

- Missing or placeholder `Item Length`, `Item Width`, or `Item Height`
- Placeholder or non-positive `Item Weight` when a weight value is present
- Implausible category for the product title
- Product-family mismatch detected by `src/utils/listing_quality_gate.py`
- Non-furniture item specifics on furniture listings
- Known mojibake in generated listing text
- Only one source image for a draft that should have multiple product images
- Empty or placeholder description
- Live eBay `product.imageUrls` count lower than the expected local source image count after publish or revise

Image handling rule:

- `src/clients/real_ebay_client.py::clean_image_url()` may remove `x-oss-process`.
- It must preserve `x-cc`, `x-cu`, `x-ct`, and `x-cs`.
- Always read back eBay Inventory after image-impacting publish or revise operations.

## MI Debug Workflow

1. Check `python scheduler_daemon.py --status`.
2. Inspect `logs/_scheduler_health.json`.
3. Run `python scripts/mi_diagnose.py`.
4. If you changed MI code, run the focused MI regression slice from `docs/TESTING.md`.
5. Only treat the issue as fixed after snapshot, digest, trend state, and health output all agree.

## Category Matching Notes

- `src/services/ebay_category_matcher.py` rejects obvious cross-family matches such as pet products to patio chairs, tents to tables, adirondack chairs to generic chairs, outdoor daybeds to patio chairs, and desk tops or table tops to dining tables unless the title clearly says dining table.
- `src/utils/listing_quality_gate.py` is the local product-identity layer before taxonomy. If a generated draft is clearly a coffee table, bench, storage ottoman, dining chair, patio set, or sofa, normalize that profile before trusting AI or taxonomy suggestions.
- Legacy category IDs still appear in historical data. Important remaps include `177815 -> 38204` and `177816 -> 107578`.

## Repricing Truthfulness

Three flows can change live eBay prices:

- `scripts/batch_smart_reprice.py`
- `src/plugins/inventory_sync/sync_service.py`
- `scripts/sales_health_check.py`

All three must verify the live eBay offer price after update before writing the new price back into local DB state.

If a report says repriced but the live offer disagrees, treat it as a verification failure, not a success.

## Quick Commands

```bash
python server.py
streamlit run app.py --server.port 8501
python scheduler_daemon.py --status
python tools/refresh_token.py
python scripts/audit_fix_ready_drafts.py
python batch_publish.py --dry-run
python batch_publish.py --sku YOURSKU
python tools/scan_image_collapse.py
python tools/restore_listing_images.py YOURSKU
python daily_tasks.py
python scripts/mi_diagnose.py --json
python scripts/batch_smart_reprice.py --apply --email
python scripts/sales_health_check.py --auto-fix --email
python -m pytest
```

## Files To Check First

- `server.py`
- `daily_tasks.py`
- `scheduler_daemon.py`
- `scripts/audit_fix_ready_drafts.py`
- `scripts/mi_diagnose.py`
- `src/services/ebay_category_matcher.py`
- `src/utils/listing_quality_gate.py`
- `src/utils/publish_validation.py`
- `src/clients/real_ebay_client.py`
- `src/plugins/terapeak_research/aggregation.py`
- `src/plugins/terapeak_research/history.py`
- `src/web/pages/market_intelligence.py`
- `src/plugins/inventory_sync/sync_service.py`
- `scripts/sales_health_check.py`
- `src/utils/email_sender.py`
- `docs/CRO_SYSTEM.md`
- `src/services/cro_daily_runner.py`
- `scripts/cro_sentinel.py`
- `skill.md`
