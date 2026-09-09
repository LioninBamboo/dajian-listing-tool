# Repo Hygiene Plan

## Goal

Reduce root-level noise, separate local runtime artifacts from maintained code, and make notification/email defaults match the actual operating setup.

## Current Findings

- Notification routing is intended to follow `NOTIFICATION_EMAIL`.
- Local runtime artifacts and operator scratch files are mixed into the repo root.
- The root contains too many one-off Python probes that should be classified before they remain as first-class entrypoints.
- Database WAL/SHM files, logs, temp images, and local Codex state create persistent `git status` noise.

## Phase 1: Stop The Bleeding

Status: complete

- Remove hardcoded Gmail recipient defaults from runnable code.
- Expand `.gitignore` for local Codex/runtime/temp artifacts.
- Generate a root-structure audit so cleanup is based on a concrete inventory instead of ad hoc moves.

## Phase 2: Root Entry Cleanup

Status: in progress

- Review root Python files that are not primary entrypoints.
- Move maintained operational scripts into `scripts/`.
- Move probes/diagnostics into `tools/local_diagnostics/` or `archive/debug_scripts/`.
- Leave compatibility shims only where an existing entrypoint is still referenced externally.

## Phase 3: Runtime/Data Separation

Status: pending

- Decide which root JSON/TXT files are durable working data versus disposable runtime outputs.
- Keep durable operator state under `cache/` or a dedicated runtime directory.
- Keep logs and generated reports out of the root.

## Phase 4: Archive And Docs Pass

Status: pending

- Make `archive/` consistently split between `dead_code/`, `debug_scripts/`, `docs_archive/`, and `logs/`.
- Remove duplicated or stale documentation where the same workflow exists in multiple Markdown files.
- Update top-level docs so the actual supported entrypoints are obvious.

## Constraints

- Do not delete or revert user-owned work in the dirty tree.
- Prefer additive structure and explicit classification before moving tracked files.
- Keep cleanup changes small enough to verify independently.

## This Round

- Inventory sync manual email now reads only `NOTIFICATION_EMAIL`; it no longer has a runnable Gmail recipient fallback.
- Added a regression test for notification-recipient resolution in `tests/test_inventory_sync_daily_sync_email.py`.
- Tightened `.gitignore` for local Codex/runtime/debug artifacts.
- Moved `debug_desc.py`, `fetch_debug.py`, and `check5.py` out of the repo root into `tools/local_diagnostics/`.
- Moved `_deep_check.py`, `_extract_links.py`, `_links.py`, `check_ready.py`, `check_ready_detailed.py`, `compare_ebay_gigacloud.py`, and `compare_ebay_gigacloud_batch2.py` into `tools/local_diagnostics/`.
- Moved root-only probes `check_aspects.py` and `fix_ready_drafts.py` into `tools/local_diagnostics/`.
- Moved `debug_api.json`, `debug_desc.html`, and `tmp_w773s00005_thumb.jpg` out of the repo root into `scratch/`.
- Re-ran the root audit after this batch; root items are now 51 and `root_python_candidate` is down to 2.
- `debug_server.log` is still in the repo root for now because an active process holds the file handle open.
- Active listing audit now hard-blocks full `PUBLISHED` non-`--live` `--fix`, and the JSON report persists `source=live_ebay|local_db_optimization`.
- Patched `daily_tasks.py` so scheduled active-listing auto-fix reads live inventory / offer context before revising PUBLISHED listings.
- Added a regression test for the scheduled live-fix path in `tests/test_daily_tasks_listing_audit.py`.
- Executed one residual live repair round for the 141 remaining SKUs from the 2026-06-18 postfix report and verified the next full live audit dropped to 0 issues.
- `docs/REPO_HYGIENE_AUDIT.md` has been refreshed to the current state.
