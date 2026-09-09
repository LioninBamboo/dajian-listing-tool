# Handoff Plan For Next Model

Last updated: `2026-06-26`

## Current Goal

Continue repository cleanup and commit-batch preparation without disturbing the user's existing work.

The next model should focus on:

1. preserving the current staged docs batch
2. preparing the next code batch cleanly
3. avoiding accidental stage pollution
4. not touching unrelated user changes

## Current Git State

There is a large dirty worktree.

Important: the staged index has already been cleaned down to a **docs-only batch**.

The staged docs batch currently contains these files:

- `README.md`
- `agent.md`
- `docs/ARCHITECTURE.md`
- `docs/COMMIT_BATCH_PLAN.md`
- `docs/CRO_RELIST_REVIVE_SPEC.md`
- `docs/CRO_SYSTEM.md`
- `docs/DAJIAN_API_REFERENCE.md`
- `docs/DOCUMENTATION_AUDIT.md`
- `docs/LISTING_QUALITY_GATE.md`
- `docs/REPO_HYGIENE_AUDIT.md`
- `docs/REPO_HYGIENE_PLAN.md`
- `docs/SCRIPT_MATRIX.md`
- `docs/SMART_HTML_TRUNCATION.md`
- `docs/TESTING.md`
- `docs/USAGE.md`
- `docs/decisions/ADR-001-listing-quality-gate.md`
- `docs/decisions/ADR-002-live-listing-audit-replaces-scheduled-title-optimization.md`
- `docs/marketplace_insights_request_20260330.md`
- `skill.md`

Do not casually add non-doc files into the index unless intentionally starting the next batch.

## Work Already Completed

### 1. Reports retention / email log cleanup

Implemented and verified:

- `src/utils/email_sender.py`
- `daily_tasks.py`
- `src/web/pages/market_intelligence.py`
- `docs/USAGE.md`
- `docs/ARCHITECTURE.md`
- `tests/test_email_sender_env.py`
- `tests/test_market_intelligence_scoring.py`

Behavior now:

- email-side report artifacts are auto-pruned to 3 days
- MI digest retention changed from 7 days to 3 days

Verified with:

- `.venv\Scripts\python.exe -m pytest tests/test_email_sender_env.py tests/test_market_intelligence_scoring.py -q`
- result: `109 passed`

### 2. Existing report files were cleaned

Historical report artifacts were reduced from very large volume down to recent 3-day retention only.

### 3. Commit batching analysis exists

Reference:

- `docs/COMMIT_BATCH_PLAN.md`

This file already contains:

- batch definitions
- whole-file candidates
- `git add -p` candidates
- suggested commit order

## Most Important Constraints

### Do not do these

- do not reset the worktree
- do not use `git reset --hard`
- do not discard user changes
- do not stage large mixed business files blindly
- do not disturb the current staged docs batch unless explicitly making a new batch

### Do these

- inspect `git diff --cached --name-status` before changing the index
- if starting a new batch, first intentionally clear or preserve the docs batch
- when working on code batches, isolate the smallest self-contained unit
- prefer tests for any code batch you touch

## Recommended Next Batch

The smallest clean next code batch is:

- `src/services/ebay_auth.py`
- `tests/test_ebay_auth_config.py`

Why this is the best next batch:

- it is self-contained
- it has focused test coverage
- it does not require `server.py` or `qwen_optimizer.py` to move with it
- it has already been validated

Validation already run:

- `.venv\Scripts\python.exe -m pytest tests/test_ebay_auth_config.py -q`
- result: `2 passed`

## Files To Treat As Separate Later Batches

These should not be mixed into the `ebay_auth` batch:

### Large tracked modifications

- `qwen_optimizer.py`
- `server.py`
- `src/services/ebay_video_uploader.py`

### Untracked but large new files / subsystems

- `daily_tasks.py`
- `src/web/pages/market_intelligence.py`
- many `src/services/cro_*.py`
- many `tests/test_cro_*.py`

## Practical Order For The Next Model

1. Confirm staged docs batch is still intact.
2. Decide whether to keep docs staged or temporarily unstage docs before staging code.
3. If moving to code batching, stage only:
   - `src/services/ebay_auth.py`
   - `tests/test_ebay_auth_config.py`
4. Re-run:
   - `.venv\Scripts\python.exe -m pytest tests/test_ebay_auth_config.py -q`
5. Only after that, inspect `qwen_optimizer.py`.

## Files The Next Model Should Read First

Read these before changing anything:

- `docs/COMMIT_BATCH_PLAN.md`
- `docs/HANDOFF_NEXT_MODEL.md`
- `git diff --cached --name-status` output for the live index
- `git status --short`
- `src/services/ebay_auth.py`
- `tests/test_ebay_auth_config.py`
- `src/utils/email_sender.py`
- `daily_tasks.py`
- `tests/test_email_sender_env.py`
- `tests/test_market_intelligence_scoring.py`

If continuing repository cleanup, also read:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/USAGE.md`

## Exact First Commands

Run these in order before any staging change:

1. `git diff --cached --name-status`
2. `git status --short`
3. `git diff -- src/services/ebay_auth.py`
4. `git diff -- tests/test_ebay_auth_config.py`

If the docs-only staged batch is no longer intact, stop and report that drift before doing anything else.

## Files That Must Not Be Mixed Into The Next Code Batch

Do not stage these together with `ebay_auth` unless intentionally creating a larger batch and explicitly explaining why:

- `.gitignore`
- `app.py`
- `daily_tasks.py`
- `qwen_optimizer.py`
- `server.py`
- `src/utils/email_sender.py`
- `src/web/pages/market_intelligence.py`
- any `src/services/cro_*.py`
- any `tests/test_cro_*.py`

## What The Next Model Should Treat As Already Solved

These items should be treated as completed context, not re-opened from scratch:

- GitHub latest committed `main` already matches local committed `main`
- email/report retention was changed from 7 days to 3 days
- old report artifacts were already pruned down to recent 3-day retention
- narrow regression tests for report retention and `ebay_auth` already passed

## What Still Is Not A Clean Commit Batch

These are real development areas still present in the worktree, but not yet organized into safe commit batches:

- `qwen_optimizer.py` related optimizer work
- `server.py` server/app integration work
- `daily_tasks.py` scheduler/report orchestration
- `src/web/pages/market_intelligence.py` MI page work
- broad new `src/services/cro_*.py` subsystem
- broad new `tests/` coverage for CRO and related features
- plugin and tooling additions under `src/plugins/`, `scripts/`, and `tools/`

## Prompt For Next Model

Use this prompt in the next conversation:

```text
You are continuing a repository cleanup and commit-batching task in C:\Users\poonx\Dajian_Listing_Tool.

Read these files first:
- docs/HANDOFF_NEXT_MODEL.md
- docs/COMMIT_BATCH_PLAN.md

Then run these commands before changing anything:
- git diff --cached --name-status
- git status --short

Current critical state:
- The staged index is intentionally a docs-only batch. Do not accidentally mix non-doc files into it.
- Large user changes exist across the repo. Do not discard or revert unrelated work.
- Email/report retention was already changed to 3 days and validated.

Your next objective:
1. Preserve or intentionally manage the current staged docs batch.
2. Prepare the next smallest self-contained code batch.
3. Start with src/services/ebay_auth.py and tests/test_ebay_auth_config.py.
4. Do not mix server.py, qwen_optimizer.py, or CRO files into that batch unless you first explain why.

Required behavior:
- Always inspect git diff --cached --name-status before changing the index.
- Never use git reset --hard or revert unrelated work.
- Prefer apply_patch for file edits.
- If you stage code, run the narrow relevant tests for that batch.
- If the current staged docs batch has drifted, report that immediately before doing any other staging.

If you need to continue beyond ebay_auth:
- analyze qwen_optimizer.py separately
- analyze server.py separately
- treat daily_tasks.py and src/web/pages/market_intelligence.py as larger later batches
```

## Success Condition

The next model should be able to resume with minimal drift and either:

- keep the docs batch intact and report the next code batch plan
- or cleanly isolate and validate the `ebay_auth` code batch

without corrupting the existing staged docs batch.
