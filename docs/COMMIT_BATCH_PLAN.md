# Commit Batch Plan

Current snapshot date: `2026-06-26`

## Progress

- `repo-hygiene / archive`: high confidence, near-ready.
  Evidence: mostly `D`, `A`, `R`, and untracked archive/debug files; low behavior risk.
- `docs / repo docs`: medium confidence, near-ready after splitting mixed files.
  Evidence: broad coverage across architecture, testing, usage, hygiene docs; some files are `AM`.
- `eBay / listing / MI core`: medium confidence, mid-to-late implementation.
  Evidence: tracked core files are actively modified, matching tests/utilities exist, but many files are `AM` / `MM`, so boundaries are still mixed.
- `Terapeak / inventory sync / audit tooling`: medium confidence, implementation mostly present.
  Evidence: plugin modules, audit scripts, diagnostics, and tests exist; still not batched cleanly.
- `CRO system`: broad but still pre-batch.
  Evidence: `114` new `src/services/cro_*.py`, `127` new `tests/test_cro_*.py`, `15` new `scripts/cro_*.py`, plus CRO pages and ops scripts. This is a large new subsystem with strong test surface, but it is still one uncommitted mass.

## Must Split By Hand

These files currently have mixed index/worktree content and should be reviewed with `git add -p` or explicit reset/add flow before any commit:

- `.github/copilot-instructions.md`
- `.gitignore`
- `README.md`
- `app.py`
- `batch_analyze.py`
- `batch_publish.py`
- `daily_tasks.py`
- `daily_terapeak_report.py`
- `docs/DAJIAN_API_REFERENCE.md`
- `docs/SMART_HTML_TRUNCATION.md`
- `docs/USAGE.md`
- `extension/content.js`
- `qwen_optimizer.py`
- `scheduler_daemon.py`
- `scheduler_watchdog.py`
- `scripts/audit_fix_active_listings.py`
- `scripts/batch_smart_reprice.py`
- `scripts/fix_listing_categories.py`
- `scripts/retry_failed_categories.py`
- `scripts/sales_health_check.py`
- `scripts/video/upload_all_videos.py`
- `scripts/video/upload_missing_videos.py`
- `server.py`
- `setup_scheduled_tasks.bat`
- `src/clients/dajian_client.py`
- `src/clients/real_ebay_client.py`
- `src/plugins/active_listing_optimizer/daily_optimize.py`
- `src/plugins/inventory_sync/daily_sync.py`
- `src/plugins/inventory_sync/sync_service.py`
- `src/plugins/terapeak_research/intelligence_service.py`
- `src/plugins/terapeak_research/research_client.py`
- `src/plugins/terapeak_research/trend_discovery.py`
- `src/services/ebay_ad_service.py`
- `src/services/ebay_auth.py`
- `src/services/ebay_category_matcher.py`
- `src/services/ebay_discount_service.py`
- `src/services/ebay_performance.py`
- `src/services/ebay_publisher.py`
- `src/services/ebay_video_uploader.py`
- `src/services/email_notifier.py`
- `src/services/pricing_engine.py`
- `src/services/vehicle_compatibility.py`
- `src/utils/dimension_helpers.py`
- `src/utils/email_sender.py`
- `src/utils/html_truncator.py`
- `src/web/pages/competition_monitor.py`
- `src/web/pages/inventory_management.py`
- `src/web/pages/market_intelligence.py`
- `tests/test_active_listing_audit_cli.py`
- `tools/fix_all_images.py`
- `tools/fix_specific_images.py`
- `tools/sanitize_single_value_aspects.py`

## Batch 1: Repo Hygiene / Archive

Recommended whole-file add or delete:

- `MANUAL_EXTRACTION_GUIDE.py`
- `alt_api_approach.py`
- `analyze_active_listings.py`
- `archive/batch_publish_v1.py`
- `archive/docs_archive/TOKEN_ISOLATION.md`
- `archive/debug_scripts/debug_green_sofa.py`
- `archive/debug_scripts/debug_publish.py`
- `archive/debug_scripts/debug_token.py`
- `archive/test_scripts/`
- `browse_listings.py`
- `check_all_offers.py`
- `check_offer.py`
- `check_offer_status.py`
- `check_product.py`
- `check_publish_issue.py`
- `cleanup_invalid_skus.py`
- `create_listing_legacy.py`
- `create_location_and_publish.py`
- `fast_publish.py`
- `get_active_published.py`
- `get_offers_compare.py`
- `list_inventory.py`
- `manual_auth_exchange.py`
- `nuke_offers.py`
- `publish_with_policies.py`
- `quick_check.py`
- `quick_publish.py`
- `quick_publish_demo.py`
- `reset_product.py`
- `simple_publish_with_location.py`
- `streamlit_app.py`
- `task.md`
- `try_all_payloads.py`
- `update_dajian_api_v2.py`
- `update_dajian_env.py`
- `update_inv_country.py`
- `update_location.py`
- `verify_ebay_auth.py`

Notes:

- This batch should not include runtime files or business logic edits.
- If you keep archive additions, do them in the same commit as the root-file deletions they replace.

## Batch 2: Docs / Repo Docs

Whole-file add candidates:

- `agent.md`
- `docs/ARCHITECTURE.md`
- `docs/CRO_RELIST_REVIVE_SPEC.md`
- `docs/CRO_SYSTEM.md`
- `docs/DOCUMENTATION_AUDIT.md`
- `docs/LISTING_QUALITY_GATE.md`
- `docs/REPO_HYGIENE_AUDIT.md`
- `docs/REPO_HYGIENE_PLAN.md`
- `docs/SCRIPT_MATRIX.md`
- `docs/TESTING.md`
- `docs/decisions/`
- `docs/marketplace_insights_request_20260330.md`
- `pytest.ini`
- `skill.md`

Need `git add -p` because staged/unstaged are mixed:

- `README.md`
- `docs/DAJIAN_API_REFERENCE.md`
- `docs/SMART_HTML_TRUNCATION.md`
- `docs/USAGE.md`

## Batch 3: eBay / Listing / MI Core

Whole-file add candidates:

- `src/clients/ebay_client.py`
- `src/clients/ebay_trading_client.py`
- `src/services/ebay_policy_manager.py`
- `src/services/ebay_returns_adapter.py`
- `src/utils/listing_quality_gate.py`
- `src/utils/llm_fact_checker.py`
- `src/utils/mi_draft_origin.py`
- `src/utils/mi_opportunity_flow.py`
- `src/utils/publish_aspect_completion.py`
- `src/utils/publish_autofix.py`
- `src/utils/publish_validation.py`
- `src/utils/title_sanitizer.py`

Need `git add -p`:

- `app.py`
- `batch_analyze.py`
- `batch_publish.py`
- `daily_tasks.py`
- `qwen_optimizer.py`
- `server.py`
- `src/clients/dajian_client.py`
- `src/clients/real_ebay_client.py`
- `src/services/ebay_ad_service.py`
- `src/services/ebay_auth.py`
- `src/services/ebay_category_matcher.py`
- `src/services/ebay_discount_service.py`
- `src/services/ebay_performance.py`
- `src/services/ebay_publisher.py`
- `src/services/ebay_video_uploader.py`
- `src/utils/dimension_helpers.py`
- `src/web/pages/competition_monitor.py`
- `src/web/pages/inventory_management.py`
- `src/web/pages/market_intelligence.py`

## Batch 4: Terapeak / Inventory Sync / Audit Tooling

Whole-file add candidates:

- `scripts/audit_active_listing_dimensions.py`
- `scripts/audit_fix_ready_drafts.py`
- `scripts/audit_published_quantities.py`
- `scripts/fix_measurement_quality_issues.py`
- `scripts/mi_diagnose.py`
- `src/plugins/inventory_sync/__init__.py`
- `src/plugins/terapeak_research/__init__.py`
- `src/plugins/terapeak_research/_cache.py`
- `src/plugins/terapeak_research/aggregation.py`
- `src/plugins/terapeak_research/blacklist.py`
- `src/plugins/terapeak_research/external_discovery.py`
- `src/plugins/terapeak_research/history.py`
- `src/plugins/terapeak_research/performance_bridge.py`
- `src/plugins/terapeak_research/research_cli.py`
- `src/plugins/terapeak_research/scoring.py`
- `tools/local_diagnostics/`
- `tools/probe_all_dajian_integrations.py`
- `tools/probe_auto_discover.py`
- `tools/probe_client_methods.py`
- `tools/probe_dajian_api.py`
- `tools/probe_ebay_api.py`
- `tools/probe_new_features.py`
- `tools/probe_product_apis.py`
- `tools/probe_qwen_api.py`
- `tools/probe_terapeak.py`

Need `git add -p`:

- `scripts/audit_fix_active_listings.py`
- `scripts/batch_smart_reprice.py`
- `scripts/fix_listing_categories.py`
- `scripts/retry_failed_categories.py`
- `scripts/sales_health_check.py`
- `src/plugins/inventory_sync/daily_sync.py`
- `src/plugins/inventory_sync/sync_service.py`
- `src/plugins/terapeak_research/intelligence_service.py`
- `src/plugins/terapeak_research/research_client.py`
- `src/plugins/terapeak_research/trend_discovery.py`
- `tools/fix_all_images.py`
- `tools/fix_specific_images.py`
- `tools/sanitize_single_value_aspects.py`

## Batch 5: CRO System

This batch is exact but too large to safely land as one commit. Current scope is:

- All current dirty files matching `src/services/cro_*.py` (`114` files)
- All current dirty files matching `tests/test_cro_*.py` (`127` files)
- All current dirty files matching `scripts/cro_*.py` (`15` files)
- `scripts/ad_blacklist_cleanup.py`
- `scripts/ad_restore_audit.py`
- `scripts/bid_rollback_audit.py`
- `src/web/pages/cro_loop.py`
- `src/web/pages/cro_status.py`

Recommended sub-batches:

1. `CRO core primitives`
   - `src/services/cro_action_queue.py`
   - `src/services/cro_event_bus.py`
   - `src/services/cro_reasoning_trace.py`
   - `src/services/cro_queue_with_trace.py`
   - `src/services/cro_tenant_config.py`
   - `src/services/cro_snapshot_store.py`
   - `src/services/cro_secret_manager.py`
   - matching tests
2. `CRO diagnostics / control plane`
   - `src/services/cro_agent_diagnose.py`
   - `src/services/cro_alert_*`
   - `src/services/cro_dashboard_*`
   - `src/services/cro_ops_*`
   - `src/services/cro_slo_dashboard.py`
   - matching tests
3. `CRO strategy / execution`
   - `src/services/cro_promote_*`
   - `src/services/cro_returns_*`
   - `src/services/cro_inventory_*`
   - `src/services/cro_pricing_*`
   - `src/services/cro_relist_lifecycle.py`
   - `src/services/cro_auto_executor.py`
   - `src/services/cro_publish_gate.py`
   - matching tests and scripts
4. `CRO scripts / pages`
   - `scripts/cro_*.py`
   - `scripts/ad_*.py`
   - `scripts/bid_*.py`
   - `src/web/pages/cro_loop.py`
   - `src/web/pages/cro_status.py`

Because almost all of CRO is currently `??`, it can usually be added whole-file within those sub-batches.

## Whole-File Add Rule

For the current tree snapshot:

- Any file listed above under `Whole-file add candidates` can be staged as a whole file.
- Any dirty file not listed in `Must Split By Hand` can usually be staged as a whole file if you keep it inside its owning batch.

## Git Add -p Rule

Use `git add -p` for:

- every file listed in `Must Split By Hand`
- any file where you intend to separate archive/docs concerns from behavior changes
- `README.md` and the three `docs/*.md` files that are already `AM`

## Suggested Commit Order

1. `chore: archive deprecated root scripts`
2. `docs: add repo architecture and hygiene docs`
3. `feat: harden ebay auth publish and listing quality flow`
4. `feat: add MI, Terapeak, and inventory sync tooling`
5. `feat: introduce CRO core primitives`
6. `feat: add CRO diagnostics and strategy modules`
7. `test: add CRO regression coverage`
