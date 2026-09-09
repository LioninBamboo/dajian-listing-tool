# Testing Guide

This repository has three different kinds of validation assets. They are not interchangeable.

## 1. Canonical Automated Suite

Run this from the repository root:

```bash
python -m pytest
```

`pytest.ini` makes that command collect only files under `tests/` and only for these filename patterns:

- `test_*.py`
- `check_*.py`

Current maintained automated suite at the top level of `tests/`:

- `tests/test_ad_restore_audit.py`
- `tests/test_collection_analysis_regressions.py`
- `tests/test_inventory_image_regressions.py`
- `tests/test_listing_quality_gate.py`
- `tests/test_market_intelligence_scoring.py`
- `tests/test_mi_draft_origin.py`
- `tests/test_mi_diagnose.py`
- `tests/test_mi_e2e_smoke.py`
- `tests/test_pricing_floor_unification.py`
- `tests/test_runtime_cache_paths.py`
- `tests/test_scheduler_daemon_power.py`
- `tests/test_scheduler_mi_self_check.py`
- `tests/check_batch_smart_reprice_email.py`
- `tests/check_daily_tasks_smart_reprice.py`

Rules for files in `tests/`:

- They should be deterministic.
- They should not require live eBay, Dajian, Qwen, or Terapeak access.
- They should fail because of a code regression, not because an external service changed state.

## 2. Listing Generation / Publish Quality Slice

When touching generated listing data, category matching, READY audit, image handling, or publish blockers, run:

```bash
python -m pytest tests/test_listing_quality_gate.py tests/test_collection_analysis_regressions.py tests/test_inventory_image_regressions.py -q
```

Current coverage map:

- `tests/test_listing_quality_gate.py`: product-family quality gate, forbidden item specifics, mojibake cleanup, missing measurement and one-image blockers, and entrypoint wiring checks
- `tests/test_collection_analysis_regressions.py`: category matcher, measurement extraction, placeholder handling, Set Includes inference, and publish-flow regressions
- `tests/test_inventory_image_regressions.py`: EPS image normalization, supplier URL handling, image collapse detection, and active audit measurement behavior

Recommended workflow for listing-quality changes:

1. Add or update a regression that reproduces the bad SKU pattern.
2. Run the focused slice above.
3. Run `python scripts/audit_fix_ready_drafts.py` against the local DB if the change affects READY normalization.
4. Run `python batch_publish.py --dry-run` before any live publish.

Do not rely on live eBay publish as the first proof that a listing-quality change works.

## 3. MI Regression Slice

When touching `daily_tasks.py`, `scheduler_daemon.py`, `scripts/mi_diagnose.py`, `src/web/pages/market_intelligence.py`, or `src/plugins/terapeak_research/`, run the focused MI slice first:

```bash
python -m pytest tests/test_market_intelligence_scoring.py tests/test_mi_e2e_smoke.py tests/test_mi_draft_origin.py tests/test_scheduler_mi_self_check.py tests/test_mi_diagnose.py -q
```

Current MI coverage map:

- `tests/test_market_intelligence_scoring.py`: scoring, cache, blacklist, and suppression-oriented regressions
- `tests/test_mi_e2e_smoke.py`: F31 end-to-end smoke covering snapshot, digest archive, and day-over-day output
- `tests/test_mi_draft_origin.py`: MI 自动起草来源标记与 UI 检出逻辑
- `tests/test_scheduler_mi_self_check.py`: F32 scheduler health checks for missing snapshot, missing digest, empty or stale trend history, and missing reports directory
- `tests/test_mi_diagnose.py`: F33 diagnostic report structure and `--json` output

Recommended workflow for MI changes:

1. Run the MI-focused subset above.
2. If the change also touches shared scheduler or daily workload code, run full `python -m pytest` afterward.
3. If the change affects live third-party behavior, use the manual probes only after deterministic tests are green.

## 4. CRO Regression Slices

When touching `src/services/cro_*.py`, `scripts/cro_*.py`, or the CRO surfaces inside `daily_tasks.py`, run the smallest affected `tests/test_cro_*.py` files first.

For the S131-S140 ops / governance closure layer, use this focused slice:

```bash
python -m pytest tests/test_cro_canary_release.py tests/test_cro_ops_runbook.py tests/test_cro_ab_finalize.py tests/test_cro_postmortem_generator.py tests/test_cro_oncall_rotation.py tests/test_cro_data_retention.py tests/test_cro_capacity_planning.py tests/test_cro_disaster_recovery.py tests/test_cro_compliance_audit.py tests/test_cro_continuous_improvement.py tests/test_cro_ops_control_plane.py tests/test_scheduler_cro_ops.py -q
```

Guidance for CRO work:

- Pure service-layer CRO changes should prove themselves with matching `tests/test_cro_*.py` files before full regression.
- Scheduler or `daily_tasks.py` changes that affect CRO should also run full `python -m pytest` after the focused slice.
- If a CRO live probe finds a stable bug, convert it into a deterministic `tests/test_cro_*.py` regression.

When changing root cleanup rules or root file classification, run:

```bash
python -m pytest tests/test_root_structure_audit.py -q
```

## 5. Manual Integration Probes

Files under `tools/` whose names start with `probe_` are manual probes for real credentials, network access, and live third-party behavior. They are intentionally excluded from root-level `pytest`.

Current manual integration probes include:

- `tools/probe_all_dajian_integrations.py`
- `tools/probe_auto_discover.py`
- `tools/probe_client_methods.py`
- `tools/probe_dajian_api.py`
- `tools/probe_ebay_api.py`
- `tools/probe_new_features.py`
- `tools/probe_product_apis.py`
- `tools/probe_qwen_api.py`
- `tools/probe_terapeak.py`

Run these explicitly when you want live verification:

```bash
python tools/probe_ebay_api.py
python tools/probe_dajian_api.py
python tools/probe_qwen_api.py
```

Rules for manual probes:

- Expect real `.env` credentials.
- Expect network access and live rate limits.
- Do not treat them as CI blockers.
- If a live probe exposes a stable regression pattern, add a mock-based regression test under `tests/`.

## 6. Archived Debug Scripts

Historical scripts under `archive/temp_scripts/` and `archive/test_scripts/` are not part of the maintained suite.

Reasons:

- Some execute work at import time.
- Some assume specific local database state.
- Some hit live APIs directly.
- Some can mutate real records.

Example: `archive/temp_scripts/test_analyze.py` exits during import when there is no `COLLECTED` product, which is why root-level `pytest` used to crash before `pytest.ini` was added.

Treat archived scripts as reference material or one-off diagnostics only.

## 7. Local Diagnostic Scripts

Files under `tools/local_diagnostics/` are local-only diagnostic or repair helpers. They are intentionally outside the maintained automated suite.

Rules for `tools/local_diagnostics/`:

- They may assume local database state, real credentials, or one-off operator intent.
- They should not be imported by the main application flow.
- They should not be collected by root `pytest`.
- If one of them proves a stable regression, move the behavior into a deterministic test under `tests/`.

## 8. Where New Tests Belong

When adding a new automated regression:

1. Put it under `tests/`.
2. Use `test_*.py` or `check_*.py`.
3. Mock external services unless the purpose is explicitly a manual live probe.
4. If the change affects MI scheduling, digest generation, or diagnostic output, add or extend the MI-focused slice rather than hiding the regression in a generic test file.
5. If the change affects generated listing quality, category matching, measurements, item specifics, or image collapse behavior, add or extend the listing-quality slice.

When adding a new live probe:

1. Put it under `tools/`.
2. Name it with the `probe_*.py` pattern.
3. Do not rely on root-level `pytest` to run it.
4. Document what credentials or environment it needs.
