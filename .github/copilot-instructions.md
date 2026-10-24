# Dajian Listing Tool — Copilot Instructions

Use this file to get productive quickly without re-deriving the repository rules.

## Read These First

1. `README.md`
2. `agent.md`
3. `skill.md`
4. `docs/ARCHITECTURE.md`
5. `docs/TESTING.md`

## Current System Shape

- Browser extension collects supplier pages and POSTs to `POST /api/collect`.
- `server.py` stores products in `ebay_collection.db` and exposes publish / OAuth APIs.
- `qwen_optimizer.py` and `daily_tasks.py` turn `COLLECTED` products into `READY` drafts.
- `src/utils/listing_quality_gate.py` normalizes generated listing data and blocks unsafe drafts before `READY` / publish.
- `scripts/audit_fix_ready_drafts.py` is the required pre-publish audit step.
- `batch_publish.py` and `POST /api/publish/{sku}` publish drafts to eBay.
- Post-publish maintenance runs through inventory sync, smart repricing, listing audit, health checks, taxonomy repair, and title optimization.
- CRO ops/governance observation runs through `scripts/cro_ops_snapshot.py`, `scheduler_daemon.py --task cro_ops`, and the Streamlit `🚦 CRO 状态` page.

## Runtime Entrypoints

- `start.bat` starts FastAPI, Streamlit, the scheduler daemon, and a watchdog check.
- `scheduler_daemon.py` is the current scheduling entrypoint.
- `daily_tasks.py` is the daily workload entrypoint.
- `scheduler_watchdog.py` keeps the daemon alive.

Current scheduler cadence:

- `09:00` title optimization
- `09:30` full daily tasks
- `09:35` MI self-check
- `09:40` ad restore audit
- `09:45-10:30` CRO cleanup / guard / queue consume / image / specifics / promote / sentinel
- Sun `03:00` CRO ops snapshot + DB DR drill
- every `2h` analyze-only pass
- `20:00` sales health check
- every `6h` promotion rotation

## Non-Negotiable Rules

- Do not publish READY drafts before running `scripts/audit_fix_ready_drafts.py`.
- Generated drafts must pass `src/utils/listing_quality_gate.py`; any unresolved `quality_gate` issue is a publish blocker.
- `Item Length` / `Item Width` / `Item Height` cannot be placeholder values.
- `Item Weight` is optional, but if present it must be real and non-placeholder.
- Do not emit non-furniture item specifics such as `Type=Planter`, `Material=Metal`, or `Color=Black` for furniture listings unless the source really says so.
- `packageWeightAndSize` must use trustworthy package dimensions / package weight.
- OAuth tokens live in `ebay_tokens.db` through `EbayOAuthService`, not env access tokens.
- JSON fields on SQLAlchemy models require `flag_modified()` after in-place updates.
- Do not strip GigaB2B signed image query params: `x-cc`, `x-cu`, `x-ct`, `x-cs`.
- After image-impacting publish / revise operations, read back eBay Inventory `product.imageUrls`.
- If local DB has multiple source images and live eBay has only one, treat it as a publish / revise image issue.
- Price-changing flows must verify the live eBay offer price before writing success back to local DB.
- CRO ops snapshots are observation/advisory only; they must not publish, delist, reprice, or promote listings directly.
- Root cleanup starts with `tools/root_structure_audit.py`; do not bulk move files before checking callers and compatibility paths.

## Where To Implement Things

- New external API integrations: `src/clients/`
- Business rules and publishing logic: `src/services/`
- Shared validation: `src/utils/publish_validation.py`
- Listing-generation quality rules: `src/utils/listing_quality_gate.py`
- Scheduled or repair workflows: `scripts/` or `src/plugins/`
- UI pages: `src/web/pages/`

Avoid:

- Duplicating category or measurement rules in multiple scripts
- Forking product-family rules outside `listing_quality_gate.py` or `src/services/ebay_category_matcher.py`
- Bypassing `PricingEngine`, `EbayCategoryMatcher`, or `EbayPublisher`
- Treating archived scripts as current behavior

## Main Files To Check

- `server.py`
- `batch_publish.py`
- `daily_tasks.py`
- `scheduler_daemon.py`
- `scripts/audit_fix_ready_drafts.py`
- `scripts/audit_fix_active_listings.py`
- `scripts/repair_published_taxonomy.py`
- `scripts/batch_smart_reprice.py`
- `scripts/sales_health_check.py`
- `src/clients/real_ebay_client.py`
- `src/services/ebay_category_matcher.py`
- `src/services/ebay_publisher.py`
- `src/utils/listing_quality_gate.py`
- `src/utils/publish_validation.py`
- `src/services/cro_ops_control_plane.py`
- `src/plugins/inventory_sync/sync_service.py`
- `src/web/pages/cro_status.py`

## Commands

```bash
python server.py
streamlit run app.py --server.port 8501
python scheduler_daemon.py --status
python scheduler_daemon.py --task cro_ops
python tools/refresh_token.py
python scripts/audit_fix_ready_drafts.py
python batch_publish.py --dry-run
python batch_publish.py --sku YOURSKU
python tools/scan_image_collapse.py
python tools/restore_listing_images.py YOURSKU
python scripts/cro_ops_snapshot.py --dr-drill --brief logs/cro_weekly_improvement.md
python tools/root_structure_audit.py --output reports/root_structure_audit.json --markdown reports/root_structure_audit.md
python scripts/batch_smart_reprice.py --apply --email
python scripts/sales_health_check.py --auto-fix --email
python -m pytest tests/test_listing_quality_gate.py tests/test_collection_analysis_regressions.py tests/test_inventory_image_regressions.py -q
python -m pytest
```

## Testing And Probes

- Root `pytest` is intentionally limited to `tests/` by `pytest.ini`.
- Files under `tools/probe_*.py` are manual live probes and are not part of automated regression coverage.
- If a live probe reveals a stable bug, add a deterministic regression test under `tests/`.
