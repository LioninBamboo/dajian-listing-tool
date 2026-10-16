# Dajian Listing Tool

An eBay operations stack for Dajian and GigaCloud products. The current system covers collection, AI draft generation, READY audit, live publishing, post-publish maintenance, a CRO closed loop for post-publish conversion improvement, and a Market Intelligence (MI) pipeline with snapshotting, alerting, daily digest email, and self-diagnostics.

## System Shape

- Browser extension collects supplier product data and posts it to `POST /api/collect`.
- `server.py` stores products in `ebay_collection.db` and exposes API entrypoints for publish, delist, OAuth, and status checks.
- `qwen_optimizer.py` and `daily_tasks.py` turn `COLLECTED` products into draft listing content.
- `src/utils/listing_quality_gate.py` normalizes generated listing data before `READY` and again before publish.
- `scripts/audit_fix_ready_drafts.py` is the required pre-publish audit step.
- `batch_publish.py` and `POST /api/publish/{sku}` publish validated drafts to eBay.
- `daily_tasks.py` is the daily workload entrypoint for analysis, sync, repricing, audit, health checks, CRO diagnose, MI snapshotting, and summary email.
- `scheduler_daemon.py` is the long-running scheduler that triggers the workload, CRO queue consumers, live listing audit, MI self-check, ad restore, health check, and promotion rotation.

## Runtime Entrypoints

Main local entrypoints:

```bash
start.bat
python scheduler_daemon.py --status
python server.py
streamlit run app.py --server.port 8501
python tools/refresh_token.py
```

Windows helpers:

- `start.bat` starts FastAPI, Streamlit, the scheduler daemon, and the scheduler watchdog.
- `stop.bat` stops those services.
- `setup_scheduled_tasks.bat` registers the Windows scheduled tasks when they are missing.

Current scheduler cadence in `scheduler_daemon.py`:

- `09:30` full `daily_tasks.py`
- `09:40` ad restore audit
- `10:05` MI self-check
- `11:30` live `listing_audit` (`scripts/audit_fix_active_listings.py --live --email`, detect-only)
- every `2h` analyze-only pass
- `20:00` sales health check
- every `6h` promotion rotation

Scheduled title optimization is intentionally disabled. `src/plugins/active_listing_optimizer/daily_optimize.py` now hard-stops unless `ENABLE_SCHEDULED_TITLE_OPTIMIZATION` is explicitly set.

Additional CRO cadence:

- `09:45` ad blacklist cleanup
- `09:50` guard anomaly alert
- `09:55` CRO effect-audit gate
- `10:00` CRO queue price-drop consume
- `10:15` CRO image refresh
- `10:20` CRO specifics fill
- `10:30` CRO sentinel alert
- Sun `02:00` threshold learning, Sun `02:30` threshold promotion
- Sun `03:00` CRO ops snapshot, capacity sample, and DB DR drill
- Tue `10:00` smart bid, Tue `10:30` CRO promote, Tue `11:00` bid rollback
- Mon `11:00` CRO delist email

## Safe Publish Workflow

Do not publish READY drafts directly from stale draft data.

1. Run `python scripts/audit_fix_ready_drafts.py`.
2. Inspect the latest `logs/ready_draft_audit_*.json`.
3. Run `python batch_publish.py --dry-run`.
4. Publish only the drafts that pass validation.

The generated-listing quality gate runs in `batch_analyze.py`, `daily_tasks.py`, `server.py`, `scripts/audit_fix_ready_drafts.py`, and `batch_publish.py`. It repairs or blocks product-family mistakes before they can become live listings.

## Live Audit Semantics

The scheduled `11:30` active-listing audit is a live detection pass, not a silent repair pass.

- `logs/listing_audit_fix_*.json -> total_with_issues` counts unique listings with at least one issue.
- A single listing can carry multiple issue tags, so issue instances can be higher than listing rows.
- The scheduled job currently runs `scripts/audit_fix_active_listings.py --live --email`; it reports and emails, but does not auto-fix the full live corpus.
- If the daily audit count stays high after a publish batch, split the report into historical live debt versus same-day publishes that were immediately re-flagged by stricter live-audit rules.

Recent concrete example: `logs/listing_audit_fix_20260617_113018.json` reported `512` listings with issues. That was not `512` duplicate rows of one SKU. The live report contained `532` issue instances across `512` listings because `18` listings had multiple issue tags. The breakdown was:

- `497` historical live listings: `469 hallucinated_foldable`, `21 missing_video`, `13 category_mismatch`, `4 incomplete_title`
- `15` listings published on `2026-06-17`: `15 assembly_description_missing`, `8 missing_foldable`, `2 non_applicable_aspect`

Operational meaning: the near-500 report was mostly historical live debt, plus a smaller publish-vs-live rule mismatch exposed by the same-day publish batch.

Title safety rule:

- Never hard-truncate titles with raw `[:80]`.
- All publish and post-publish maintenance paths must normalize titles through `src/utils/title_sanitizer.py` so the final eBay title is <= 80 characters without ending on a broken token.

Current publish blockers:

- Missing or placeholder `Item Length`, `Item Width`, or `Item Height`
- Placeholder or non-positive measurement values, including `Item Weight` when present
- Implausible category or product-family mismatches
- Furniture item specifics that belong to other product families, such as shoe, sport, or pet fields
- Known mojibake in titles, descriptions, or item specifics
- Local source image count below the minimum required for a safe draft
- Empty or placeholder description HTML
- Live eBay image count lower than the expected source image count after publish or revise

Shipping and image rules:

- `Item Weight` is optional when no trustworthy product weight exists, but it must never be fabricated.
- Shipping weight and package dimensions belong in `packageWeightAndSize` using trusted package data.
- Publishing may remove known transformation parameters such as `x-oss-process`, but must preserve GigaB2B signed parameters `x-cc`, `x-cu`, `x-ct`, and `x-cs`.
- After publish or revise, read back eBay Inventory `product.imageUrls`. If local DB has multiple source images and live eBay has only one, treat it as a publish or revise failure.

## Market Intelligence

MI is now a first-class subsystem.

- `daily_tasks.run_mi_snapshot()` runs during the default daily workload. There is currently no standalone `--mi-only` CLI switch.
- During `run_mi_snapshot()` and the Streamlit MI page's `🚀 开始发掘爆品` action, discovered `PENDING` / `COLLECTED` opportunities are automatically prepared into local `READY` drafts in the background.
- Those MI auto-draft steps do not publish. Publish remains a separate audit + manual or button-triggered action.
- `daily_tasks.py` writes MI snapshots to `reports/mi_opportunities_*.json` and archives digest HTML to `reports/mi_digest_*.html`.
- Long-window state is stored in `reports/mi_long_window_history.json`.
- Alert suppression and operator blacklist state live in `reports/mi_alerts_state.json` and `reports/mi_blacklist.json`.
- MI alert emails and daily digest emails are Chinese and include product thumbnails.
- `scheduler_daemon.py` runs an MI health check at `10:05` and records the result in `logs/_scheduler_health.json`.
- `scripts/mi_diagnose.py` is the operator diagnostic entrypoint and supports both text output and `--json`.
- The Streamlit dashboard marks MI-origin READY drafts so operators can review them before publish.

Useful MI commands:

```bash
python daily_tasks.py
python scripts/mi_diagnose.py
python scripts/mi_diagnose.py --json
```

## CRO Closed Loop

The CRO subsystem is now a first-class post-publish workflow.

- `daily_tasks.run_cro_diagnose()` writes funnel diagnostics and queues actionable work.
- `scheduler_daemon.py` consumes that work through repricing, image refresh, specifics fill, promotion, and alerting steps.
- Service-layer ops and governance helpers now cover canary release, runbooks, A/B finalization, postmortems, on-call rotation, retention, capacity planning, disaster recovery, compliance audit, and continuous improvement.
- `scripts/cro_ops_snapshot.py` and `python scheduler_daemon.py --task cro_ops` generate the production CRO ops snapshot used by the `🚦 CRO 状态` UI.
- See `docs/CRO_SYSTEM.md` for the current CRO runtime map and the intended public interfaces for the S131-S140 closure layer.

## Key Commands

```bash
python -m pytest
python scripts/audit_fix_ready_drafts.py
python batch_publish.py --dry-run
python batch_publish.py --sku YOURSKU
python scripts/audit_fix_active_listings.py --live --fix --sku-file logs/residual_listing_audit_skus.txt
python tools/scan_image_collapse.py
python tools/restore_listing_images.py YOURSKU
python daily_tasks.py
python daily_tasks.py --analyze-only
python daily_tasks.py --sync-only
python daily_tasks.py --audit-only
python scripts/mi_diagnose.py --json
python scheduler_daemon.py --task cro_ops
python scripts/cro_ops_snapshot.py --dr-drill --brief logs/cro_weekly_improvement.md
python tools/root_structure_audit.py --output reports/root_structure_audit.json --markdown reports/root_structure_audit.md
python scripts/repair_published_taxonomy.py --apply
python scripts/batch_smart_reprice.py --apply --email
python scripts/sales_health_check.py --auto-fix --email
```

## Repository Map

- Entry points: `server.py`, `app.py`, `batch_publish.py`, `daily_tasks.py`, `scheduler_daemon.py`, `scheduler_watchdog.py`, `qwen_optimizer.py`
- Operational scripts: `scripts/` for READY audit, active-listing audit, taxonomy repair, repricing, CRO execution, health checks, ad restore, and MI diagnostics
- Core business logic: `src/services/` for auth, category matching, publishing rules, pricing, and the CRO service layer under `src/services/cro_*.py`
- External clients: `src/clients/` for Dajian, eBay REST, and Trading API access
- Automation plugins: `src/plugins/` for inventory sync, active-listing optimization, and Terapeak-based market research
- Persistence and utilities: `src/db/`, `src/utils/`, `logs/`, `reports/`, and `cache/`
- UI surfaces: `extension/`, `src/web/`, and `app.py`
- Local diagnostics: `tools/local_diagnostics/` for one-off operator helpers that should not live in the repo root
- Root cleanup planning: `tools/root_structure_audit.py` for non-destructive root inventory before moving files

Generated artifact note:

- `logs/` and `reports/` are runtime output locations and should stay out of normal source-control review.
- Runtime caches now write to `cache/`, including `cache/ad_data_cache.json` and `cache/performance_cache.json`.
- Legacy root cache filenames are read-compatible fallbacks only.

## Repricing And Email Truthfulness

- Smart repricing is implemented in `scripts/batch_smart_reprice.py` and also participates in the scheduled daily workload.
- The blanket catalog reprice is shared across all store instances and runs Monday and Thursday only. SKUs with a sale in the last 14 days are held (`REPRICE_SALES_COOLDOWN_DAYS`, default `hold`). Conversion-driven CRO queue consume stays a separate daily path.
- Repricing results are included in the daily summary email.
- Standalone smart repricing email can also be sent through the shared SMTP sender.
- `SMART_REPRICE_EMAIL_ENABLED=0` disables the extra standalone repricing email in the daily flow.

Price state must only be written back after the live eBay offer price verifies successfully. This applies to:

- `scripts/batch_smart_reprice.py`
- `src/plugins/inventory_sync/sync_service.py`
- `scripts/sales_health_check.py`

## Important Files

```text
server.py                                   FastAPI API and server-side publish flow
app.py                                      Streamlit operations UI
batch_publish.py                            READY draft publisher
daily_tasks.py                              daily workload entrypoint, including MI snapshot
scheduler_daemon.py                         long-running scheduler and MI self-check
scheduler_watchdog.py                       scheduler watchdog
qwen_optimizer.py                           AI optimization logic
scripts/audit_fix_ready_drafts.py           READY-draft audit and normalization
scripts/mi_diagnose.py                      MI diagnostics and JSON report output
scripts/batch_smart_reprice.py              market-based repricing
scripts/sales_health_check.py               price mismatch and listing health diagnostics
src/services/ebay_category_matcher.py       category and item-specific matching
src/services/ebay_publisher.py              category defaults and publishing rules
src/clients/real_ebay_client.py             eBay Inventory/Offer client and image URL cleaning
src/utils/listing_quality_gate.py           generated-listing quality gate
src/utils/publish_validation.py             shared measurement/category validation
src/plugins/inventory_sync/sync_service.py  inventory and price sync
src/web/pages/market_intelligence.py        Streamlit MI page
src/utils/email_sender.py                   unified SMTP sender
agent.md                                    concise agent guardrails
skill.md                                    consolidated functional map and constraints
```

## Environment Variables

Required in `.env`:

```env
EBAY_APP_ID=your_app_id
EBAY_CERT_ID=your_cert_id
EBAY_DEV_ID=your_dev_id
EBAY_ENVIRONMENT=PRODUCTION
EBAY_REDIRECT_URI=your_registered_redirect_uri

DAJIAN_API_KEY=your_dajian_key
DAJIAN_API_SECRET=your_dajian_secret

QWEN_API_KEY=your_qwen_api_key

NOTIFICATION_EMAIL=your_email@example.com
NOTIFICATION_EMAIL_PASSWORD=your_email_password
SMART_REPRICE_EMAIL_ENABLED=1
```

## Data Stores

- `ebay_collection.db`: product records, statuses, listing metadata, pricing state, and MI source inputs
- `ebay_tokens.db`: eBay OAuth tokens

Token handling note:

- Tokens are stored in SQLite and managed through `EbayOAuthService`.
- Do not assume access tokens come from environment variables.

## Documentation Map

- `docs/USAGE.md` is the operator runbook for startup, publish, MI, maintenance, and troubleshooting.
- `docs/LISTING_QUALITY_GATE.md` is the canonical generated-listing quality gate reference.
- `docs/CRO_SYSTEM.md` is the CRO runtime and governance map for diagnosis, queue consumption, and S131-S140 ops helpers.
- `docs/ARCHITECTURE.md` documents runtime topology, module boundaries, and the MI + CRO pipelines.
- `docs/TESTING.md` defines the maintained automated suite, MI regressions, CRO regression slices, and manual probes.
- `docs/DOCUMENTATION_AUDIT.md` records which Markdown files are current and what must be updated together.
- `docs/SCRIPT_MATRIX.md` lists the active, manual, debug, and legacy script entry points.
- `docs/decisions/ADR-001-listing-quality-gate.md` records the decision to centralize generated-listing safety.
- `docs/decisions/ADR-002-live-listing-audit-replaces-scheduled-title-optimization.md` records why scheduled title optimization is paused and replaced by live audit + repair.
- `agent.md` contains current guardrails for future coding agents.
- `skill.md` is the long-form functional map for maintainers and agents.
