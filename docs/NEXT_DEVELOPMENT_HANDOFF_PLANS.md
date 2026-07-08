# Next Development Handoff Plans

Last updated: `2026-06-28`

This document is for the next coding conversation. It inventories the feature
areas that are already in the repository but still need focused completion,
production validation, or follow-through work.

Important current state:

- Branch observed: `codex/live-audit-semantic-guard`.
- Working tree is not clean at the time this handoff was written.
- Existing unstaged files observed:
  - `scripts/audit_fix_active_listings.py`
  - `src/clients/real_ebay_client.py`
  - `src/utils/listing_quality_gate.py`
  - `tests/test_active_listing_audit_cli.py`
  - `tests/test_inventory_image_regressions.py`
  - `tests/test_listing_quality_gate.py`
  - `tests/test_real_ebay_client_inventory_identifiers.py`
- Existing untracked file observed:
  - `docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md`
- Treat those files as active in-progress work. Do not revert them.
- The daily 11:30 listing audit is now intended to be a live read-only,
  incremental audit. It should not run a full live fetch over every published
  listing unless an operator explicitly scopes or overrides it.
- Recent live-audit fixes already in the working tree:
  - preserve existing live `videoIds` when inventory replacement payload omits
    `video_urls`
  - strip invalid `Assembly Status=Yes/No` values while keeping
    `Assembly Required`
  - remove lingering claim-diff residues for `foldable`, `waterproof`, and
    `claimed N-seat/source has M` mismatch phrases

## Read First

Before implementing any plan below, read these files:

- `README.md`
- `agent.md`
- `skill.md`
- `docs/ARCHITECTURE.md`
- `docs/USAGE.md`
- `docs/TESTING.md`
- `docs/SCRIPT_MATRIX.md`
- `docs/LISTING_QUALITY_GATE.md`
- `docs/CRO_SYSTEM.md`
- `docs/CRO_RELIST_REVIVE_SPEC.md`
- `docs/COMMIT_BATCH_PLAN.md`
- `docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md`

Then run:

```powershell
git status --short --branch
git diff -- scripts/audit_fix_active_listings.py src/clients/real_ebay_client.py src/utils/listing_quality_gate.py tests/test_active_listing_audit_cli.py tests/test_inventory_image_regressions.py tests/test_listing_quality_gate.py tests/test_real_ebay_client_inventory_identifiers.py
```

If the working tree contains user changes, preserve them and work around them.

## Plan 1: Stabilize Listing Quality Gate And Incremental Live Audit

### Why This Is Still Open

The repository now has three overlapping listing-quality layers:

1. publish-time normalization in `src/utils/listing_quality_gate.py`
2. operator audit/fix logic in `scripts/audit_fix_active_listings.py`
3. scheduled daily live audit in `daily_tasks.py`

The most recent production issue was not that the daily job rewrote all live
listings; it was that the daily job still performed a full live fetch against
all published rows, which inflated reports with network failures and created
unnecessary link-touching risk. The current working tree already includes an
incremental daily-audit change plus the live video / assembly / claim-cleanup
fixes listed above, but the documentation and verification plan were still
describing the older full-scan behavior.

Separately, the `KEY FEATURES` fallback work in
`src/utils/listing_quality_gate.py` is still active dirty work and must be
preserved.

### Read These Files

- `src/utils/listing_quality_gate.py`
- `tests/test_listing_quality_gate.py`
- `scripts/audit_fix_active_listings.py`
- `scripts/audit_fix_ready_drafts.py`
- `batch_publish.py`
- `daily_tasks.py`
- `server.py`
- `src/clients/real_ebay_client.py`
- `tests/test_collection_analysis_regressions.py`
- `tests/test_daily_tasks_listing_audit.py`
- `tests/test_inventory_image_regressions.py`
- `tests/test_real_ebay_client_inventory_identifiers.py`
- `docs/LISTING_QUALITY_GATE.md`

### Development Steps

1. Inspect the existing dirty diff before editing.
2. Preserve the incremental daily audit behavior:
   - unchanged local `clean` rows should skip daily live fetch entirely
   - `dirty`, `pending_verify`, missing-meta, or ruleset/source-changed rows
     should remain in live audit scope
   - full live scans should require an explicit operator path rather than the
     default 11:30 scheduler run
3. Preserve the new reporting split for `live_fetch_failed`,
   `live_inventory_missing`, and `live_offer_missing`:
   - keep them in dirty-state metadata so the SKU is rechecked later
   - do not count them as content-quality failures in daily summaries
   - surface them in a separate transport/availability section in reports and
     emails
4. Confirm whether `_ensure_key_features_block()` should run for every generated listing or only when the listing would otherwise fail `description_incomplete`.
5. Ensure fallback bullets do not invent unsupported claims. Use only title, item specifics, source facts, and measured values.
6. Add or refine tests for these cases:
   - description has `<ul><li>` but no `KEY FEATURES` heading
   - description has `KEY FEATURES` heading but no bullets
   - source says `Assembly Required=Yes` and description lacks assembly wording
   - description is already valid and is not rewritten
   - unchanged clean row is skipped before any daily live fetch
   - dirty/pending-verify rows remain in incremental live scope
   - live video IDs survive inventory replacement when no explicit new video is supplied
   - invalid `Assembly Status=Yes/No` does not overwrite valid `Assembly Required`
7. Explicitly close the `fix_listing_on_ebay()` reverse-write path:
   - make `tests/test_active_listing_audit_cli.py` pass for the current dirty
     worktree
   - verify inventory-only updates, offer-description updates, republish state
     sync, and claim-cleanup write paths all still behave as expected
   - ensure audit/fix writeback uses the intended live snapshot as its base and
     does not silently drop fields during partial repair
8. Check whether active audit expects the same structure. If not, update the gate or audit test so publish-time and live-audit behavior match.
9. Update `docs/LISTING_QUALITY_GATE.md` and any operator-facing audit docs if the public rule changes.

### Acceptance Criteria

- The daily scheduled audit no longer performs a full live fetch across every
  published row by default.
- Incremental daily live audit still covers listings that are dirty,
  pending-verify, missing metadata, or locally changed.
- Transport/API fetch failures are reported separately from content-quality
  failures so network turbulence does not inflate content-error counts.
- `fix_listing_on_ebay()` reverse-write paths are covered by focused CLI tests
  and do not regress inventory fields, offer descriptions, or local publish
  state sync.
- Generated descriptions that pass publish-time quality also pass active
  audit's structure checks.
- The gate does not add unsupported marketing facts.
- Existing source-claim hallucination blockers still fire.
- Live video IDs and assembly-required data are not regressed by audit/fix
  write paths.

### Verification

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
.\.venv\Scripts\python.exe -m pytest tests/test_listing_quality_gate.py tests/test_collection_analysis_regressions.py tests/test_inventory_image_regressions.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_daily_tasks_listing_audit.py tests/test_active_listing_audit_cli.py tests/test_real_ebay_client_inventory_identifiers.py -q
.\.venv\Scripts\python.exe -m py_compile src/utils/listing_quality_gate.py scripts/audit_fix_active_listings.py scripts/audit_fix_ready_drafts.py batch_publish.py daily_tasks.py server.py
```

Run full regression if shared behavior changes:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

### Prompt For Next Conversation

```text
Finish and commit Plan 1 (listing quality gate + incremental live audit) in
C:\Users\poonx\Dajian_Listing_Tool. This plan's work is ALREADY half-done as
uncommitted edits in the working tree; your job is to complete the gaps and land
it as one clean commit.

== Branch and commit discipline (read first) ==
- Work on branch codex/live-audit-semantic-guard.
- The working tree shows ~490 dirty files: ~479 are line-ending (CRLF) noise and
  ~10 are the real Plan 1 work-in-progress + spec docs. Do NOT run a line-ending
  normalization here (that is a separate later commit).
- Stage ONLY the specific Plan 1 files you finish, with `git add <path>`. NEVER use
  `git add -A` / `git add .` (you would commit hundreds of CRLF flips).
- Before committing run BOTH:
    git diff --cached --stat                  # lists only your Plan 1 files
    git diff --cached --ignore-cr-at-eol --stat  # same set, proves no stray CRLF-only files
  Then commit, e.g. "feat(listing): stabilize quality gate + incremental live audit".
- Do NOT modify or delete existing test assertions to make the suite pass; add or
  strengthen tests instead.

== Step 0: understand what is already done ==
Run and read before editing anything:
- git status --short --branch
- git diff -- src/utils/listing_quality_gate.py scripts/audit_fix_active_listings.py daily_tasks.py src/clients/real_ebay_client.py
- git diff -- tests/test_listing_quality_gate.py tests/test_daily_tasks_listing_audit.py tests/test_active_listing_audit_cli.py tests/test_inventory_image_regressions.py tests/test_real_ebay_client_inventory_identifiers.py
Identify the ~9 real Plan 1 files (code + tests). Do not touch docs/ spec files or
.gitattributes — those belong to other commits.

Read for context:
- docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md  (Plan 1: Development Steps + Acceptance Criteria)
- docs/LISTING_QUALITY_GATE.md
- src/utils/listing_quality_gate.py
- scripts/audit_fix_active_listings.py
- daily_tasks.py

== Objective ==
Make the daily live audit incremental (not a full live fetch by default), finish the
KEY FEATURES fallback/normalization so publish-time descriptions and live-audit
structure checks agree, and close the fix_listing_on_ebay() reverse-write path with
tests — without re-introducing the old full-scan behavior or any unsupported claims.

== Operation guide (do these, in order) ==
1. Incremental daily audit (preserve, do not regress):
   - unchanged local `clean` rows skip the daily live fetch entirely
   - `dirty` / `pending_verify` / missing-meta / ruleset-or-source-changed rows stay
     in live-audit scope
   - a FULL live scan must require an explicit operator flag, never the default 11:30
     scheduler run
2. Keep the transport/availability reporting split intact:
   - `live_fetch_failed`, `live_inventory_missing`, `live_offer_missing` stay in
     dirty-state metadata so the SKU is rechecked later
   - they are NOT counted as content-quality failures in daily summaries
   - they surface in a separate transport/availability section of reports + emails
3. KEY FEATURES fallback in listing_quality_gate.py:
   - decide and document whether `_ensure_key_features_block()` runs for every
     generated listing or only when it would otherwise fail `description_incomplete`
   - fallback bullets may use ONLY title, item specifics, source facts, and measured
     values — never invented/marketing claims; existing source-claim hallucination
     blockers must still fire
4. Close the fix_listing_on_ebay() reverse-write path:
   - make tests/test_active_listing_audit_cli.py green for the current worktree
   - verify inventory-only updates, offer-description updates, republish state sync,
     and claim-cleanup write paths still behave; writeback uses the intended live
     snapshot as base and does not silently drop fields during partial repair
   - do not regress live videoIds or assembly-required data
5. Ensure publish-time gate output and live-audit structure checks agree (update the
   gate or the audit test, not by weakening assertions).
6. Update docs/LISTING_QUALITY_GATE.md only if a public rule actually changes.

== Tests to add/refine (tests/test_listing_quality_gate.py + test_daily_tasks_listing_audit.py) ==
- description has <ul><li> but no KEY FEATURES heading
- description has KEY FEATURES heading but no bullets
- source says Assembly Required=Yes but description lacks assembly wording
- a valid description is NOT rewritten
- unchanged clean row is skipped before any daily live fetch
- dirty/pending-verify rows remain in incremental live scope
- live video IDs survive inventory replacement when no explicit new video is supplied
- invalid Assembly Status=Yes/No does not overwrite valid Assembly Required

== Acceptance (must all hold) ==
- Daily scheduled audit does NOT full-live-fetch every published row by default.
- Incremental audit still covers dirty / pending-verify / missing-meta / locally-changed.
- Transport failures reported separately from content-quality failures.
- fix_listing_on_ebay() reverse-write paths covered by CLI tests; no regression to
  inventory fields, offer descriptions, or local publish-state sync.
- Gate adds no unsupported marketing facts; hallucination blockers still fire.
- Live video IDs and assembly-required data not regressed.

== Verify before finishing (use the venv, NOT bare pytest) ==
  $env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
  .\.venv\Scripts\python.exe -m pytest tests/test_listing_quality_gate.py tests/test_collection_analysis_regressions.py tests/test_inventory_image_regressions.py -q
  .\.venv\Scripts\python.exe -m pytest tests/test_daily_tasks_listing_audit.py tests/test_active_listing_audit_cli.py tests/test_real_ebay_client_inventory_identifiers.py -q
  .\.venv\Scripts\python.exe -m py_compile src/utils/listing_quality_gate.py scripts/audit_fix_active_listings.py scripts/audit_fix_ready_drafts.py batch_publish.py daily_tasks.py server.py
Then re-run the two pytest slices one more time AFTER staging, and confirm
`git diff --cached --ignore-cr-at-eol --stat` lists only your intended Plan 1 files.
```

## Plan 2: Complete CRO Relist Revive Lifecycle

### Why This Is Still Open

`docs/CRO_RELIST_REVIVE_SPEC.md` still describes the target lifecycle
correctly, but the implementation has moved past a pure Phase 1 foundation:

- detection and approval storage exist
- `precheck_approved()` exists
- `execute_prechecked_relist()` exists and already contains the safer ordering
  of local publish dry-run -> withdraw old listing -> republish -> status
  transition

What remains incomplete is not the whole execution engine; it is the operator
surface and end-to-end lifecycle wiring:

- `execute_approved(..., apply_changes=True)` still raises `NotImplementedError`
  as a deliberate gate
- `evaluate_observations()` is still a no-op selector
- `scripts/cro_relist_lifecycle.py` still exposes only `--detect` and
  `--delist-links`
- scheduler/operator flow does not yet expose the precheck/execute/evaluate
  stages safely

### Read These Files

- `docs/CRO_RELIST_REVIVE_SPEC.md`
- `docs/CRO_SYSTEM.md`
- `src/services/cro_relist_lifecycle.py`
- `scripts/cro_relist_lifecycle.py`
- `scripts/cro_delist.py`
- `batch_publish.py`
- `src/services/listing_status_sync.py`
- `tests/test_cro_relist_lifecycle.py`
- `tests/test_cro_relist_lifecycle_cli.py`
- `tests/test_cro_delist.py`
- `scheduler_daemon.py`

### Development Steps

1. Keep all live withdraw/re-publish steps behind explicit `--apply`; dry-run
   must remain the default-safe path.
2. Do not route operators through `execute_approved(apply_changes=True)` until
   it becomes a thin orchestrator over the safer two-step flow below.
3. Wire CLI commands for:
   - `--approve`
   - `--precheck`
   - `--execute`
   - `--evaluate`
4. Prefer a two-step operator path:
   - `--precheck --apply`
   - `--execute --apply`
   and only let `execute_approved()` become a thin wrapper if tests make that
   contract clearer.
5. Add observation transition logic:
   - `published_new -> observing`
   - `observing -> revived_success`
   - `observing -> needs_conversion_help -> observing` (at most once)
   - `observing -> fallback_delist_pending`
6. Observation evidence is now specified in `docs/CRO_RELIST_REVIVE_SPEC.md`
   (see the "Observation Evaluation" section and resolved Open Questions 1-2).
   Implement to that spec; do not re-derive it:
   - `revived_success` requires a `sale` (may resolve early on sale)
   - `observe_until = published_new + observe_window_days` (21 P3 / 14 P2)
   - traffic recovery without a sale -> `needs_conversion_help` (soft levers,
     re-observe once), NOT delist
   - dead traffic -> `fallback_delist_pending`
   - floors/window live in `cro_thresholds` so they ride the Plan 3 shadow gate
   - add `observe_window_days` and `conversion_help_attempts` to the schema
7. Extend or verify `scripts/cro_delist.py` includes lifecycle fallback candidates in confirmation emails.
8. Add scheduler integration only after CLI and service tests pass. The first
   scheduler hook should stay detect/precheck/evaluate only; do not auto-run
   live execute in the scheduler.

### Acceptance Criteria

- No live delist/relist happens without explicit approval and `--apply`.
- Dry-run can preview candidate, approve, precheck, execute selection, and
  evaluation without mutating the original DB.
- Failed relist does not mark the SKU as final delisted.
- Sales recheck blocks execution immediately before withdraw.
- Fallback delist remains human-confirmed.
- The public CLI reflects the actual safe two-step execution surface instead of
  implying that `execute_approved(apply_changes=True)` is the primary path.

### Verification

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
.\.venv\Scripts\python.exe -m pytest tests/test_cro_relist_lifecycle.py tests/test_cro_relist_lifecycle_cli.py tests/test_cro_delist.py -q
.\.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --detect --dry-run --limit 50 --out reports\cro_relist_lifecycle_detect_preview.json
.\.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --delist-links --dry-run --limit 50 --out reports\cro_relist_final_delist_links_preview.json --html reports\cro_relist_final_delist_links_preview.html
.\.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --precheck --dry-run --limit 20 --out reports\cro_relist_precheck_preview.json
.\.venv\Scripts\python.exe scripts\cro_relist_lifecycle.py --execute --dry-run --limit 10 --out reports\cro_relist_execute_preview.json
```

Only after deterministic tests are green, manually test one approved SKU with
operator confirmation.

### Prompt For Next Conversation

```text
Continue the CRO relist revive lifecycle feature in C:\Users\poonx\Dajian_Listing_Tool.

Branch and commit discipline (read first):
- Work on branch codex/live-audit-semantic-guard.
- The working tree already shows ~490 dirty files: ~479 are line-ending (CRLF)
  noise and ~10 are real (the Plan 1 in-progress work + spec docs). Do NOT revert
  or modify any of them; work around them.
- Commit your Plan 2 observation changes as their own dedicated commit, e.g.
  "feat(cro): phase4 observation evaluation", so the diff is reviewable in
  isolation. Do not amend, squash, or fold unrelated dirty files into it.
- The working tree contains ~479 files that differ only by line endings
  (CRLF noise), plus the Plan 1 in-progress files. Stage ONLY the specific
  files you changed with `git add <path>`. NEVER use `git add -A` / `git add .`,
  or you will commit hundreds of CRLF-only flips. Run `git diff --cached`
  before committing and confirm it lists only your intended files.

Read:
- docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md  (Plan 2)
- docs/CRO_RELIST_REVIVE_SPEC.md          (the "Observation Evaluation" section and resolved Open Questions 1-2 are authoritative; do not re-derive them)
- src/services/cro_relist_lifecycle.py
- scripts/cro_relist_lifecycle.py
- scripts/cro_delist.py
- src/services/cro_thresholds.py
- src/services/cro_action_queue.py
- tests/test_cro_relist_lifecycle.py
- tests/test_cro_relist_lifecycle_cli.py
- tests/test_cro_delist.py

Objective:
Finish the operator-safe CLI/service lifecycle flow (approve, precheck, execute
prechecked relist, evaluate observations, human-confirmed final delist), and
implement evaluate_observations() exactly per the spec's Observation Evaluation
section.

Observation work (the main new piece):
- Add schema fields observe_window_days and conversion_help_attempts.
- Set observe_window_days when a row enters observing (21 for P3, 14 for P2) and
  observe_until = published_new + observe_window_days.
- Implement evaluate_observations(): join observing rows to the latest snapshot,
  compute window metrics vs metrics_before_json, resolve each row past observe_until:
  * revived_success on a sale (may resolve early the moment a sale lands).
  * needs_conversion_help if traffic recovered above floor AND >= pre-relist
    baseline but no sale; trigger reprice / Promoted Listings via cro_action_queue,
    then re-enter observing once (conversion_help_attempts <= 1). Must NOT
    withdraw/republish.
  * fallback_delist_pending if traffic stayed dead.
- Read floors and observe_window_days from cro_thresholds (defaults:
  revive_traffic_floor_impressions=50, revive_traffic_floor_views=5). Do not hardcode.
- Add state needs_conversion_help and transition observing -> needs_conversion_help
  -> observing (max once).

Constraints:
- No live eBay mutation unless an explicit --apply path is implemented and tested.
- Dry-run must use a temporary DB copy.
- Do not bypass batch_publish.py or existing eBay safety surfaces.
- Do not make the scheduler auto-execute withdraw/re-publish.
- A SKU with any sales is never relisted or delisted by this flow.
- needs_conversion_help must never withdraw or republish; the listing ID and CTR
  history are preserved.
- Do NOT modify or delete existing tests to make the suite pass. Add new tests.
- Before finishing, run git diff on your dedicated commit and confirm it touches
  only the intended files.
- Add focused tests: observe_until gating, early sale -> revived_success,
  traffic-recovery -> needs_conversion_help (and the one-time re-entry cap),
  dead traffic -> fallback_delist_pending, thresholds read from cro_thresholds.
- Run the focused CRO lifecycle tests before finishing:
  .\.venv\Scripts\python.exe -m pytest tests/test_cro_relist_lifecycle.py tests/test_cro_relist_lifecycle_cli.py tests/test_cro_delist.py -q
```

### Plan 2 Follow-Ups (post-acceptance, low priority)

The Phase 4 observation work (commit `1df2dd7`) passed acceptance (36/36 green).
Two non-blocking nits remain from review:

1. `evaluate_observations()` in `src/services/cro_relist_lifecycle.py` has several
   `except Exception: pass` blocks that silently swallow transition failures. A
   failed state transition disappears with no trace. Replace `pass` with a logged
   warning (action_id, sku, and the exception) using the module's existing logger.
   Do not change the control flow otherwise.
2. The `needs_conversion_help` branch only enqueues `promoted_listings`. The spec's
   Observation Evaluation section lists both reprice and Promoted Listings as the
   soft levers. Also enqueue a reprice action (via the same `cro_action_queue`
   path / inventory sync repricing) so the soft-revive pass matches the spec.

Prompt for next conversation:

```text
Apply two post-acceptance follow-ups to Plan 2 in C:\Users\poonx\Dajian_Listing_Tool.

Branch and commit discipline (read first):
- Work on branch codex/live-audit-semantic-guard.
- Stage ONLY the files you change with `git add <path>`. NEVER `git add -A` / `git add .`
  (the tree has ~479 CRLF-only noise files). Run `git diff --cached` before committing.
- Commit as its own dedicated commit, e.g. "fix(cro): observation error logging + reprice soft lever".

Read:
- docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md  (Plan 2 Follow-Ups)
- docs/CRO_RELIST_REVIVE_SPEC.md          (Observation Evaluation section)
- src/services/cro_relist_lifecycle.py    (evaluate_observations)
- src/services/cro_action_queue.py
- tests/test_cro_relist_lifecycle.py

Changes:
1. In evaluate_observations(), replace the bare `except Exception: pass` blocks with
   a logged warning (action_id, sku, exception) via the module logger. Keep behavior
   otherwise identical.
2. In the needs_conversion_help branch, in addition to the existing promoted_listings
   enqueue, also enqueue a reprice action through cro_action_queue (matching the spec's
   reprice + Promoted Listings soft levers). Must NOT withdraw/republish.

Constraints:
- Do NOT modify or delete existing tests to make the suite pass. Add a test asserting
  the needs_conversion_help branch enqueues both a reprice and a promoted_listings action.
- Run before finishing:
  .\.venv\Scripts\python.exe -m pytest tests/test_cro_relist_lifecycle.py tests/test_cro_relist_lifecycle_cli.py tests/test_cro_delist.py -q
```

## Plan 3: Finish CRO Threshold Shadow Two-Stage Promotion

### Why This Is Still Open

`scripts/cro_threshold_shadow.py` contains the comparison algorithm, but its
CLI still uses `new_thr = old_thr` with a comment that real two-stage learning
is not wired yet. This means weekly threshold promotion has guard logic in
tests and docs, but the CLI path is not a real end-to-end shadow gate.

### Read These Files

- `scripts/cro_threshold_shadow.py`
- `scripts/cro_threshold_feedback.py`
- `scripts/cro_promote_thresholds.py`
- `src/services/cro_thresholds.py`
- `src/services/conversion_diagnoser.py`
- `tests/test_cro_thresholds.py`
- `tests/test_cro_threshold_feedback.py`
- `tests/test_cro_thresholds_two_stage.py`
- `tests/test_cro_threshold_tuning.py`
- `tests/test_cro_threshold_seasonal.py`
- `docs/CRO_SYSTEM.md`

### Development Steps

1. Inspect how pending thresholds are stored by `cro_thresholds.py`.
2. Make `cro_threshold_shadow.py` load current production thresholds and proposed pending thresholds separately.
3. Feed a realistic product and market-data sample into `shadow_compare()`.
4. Ensure promote only proceeds when `safe_to_promote` is true.
5. Write a JSON report under `reports/`.
6. Update scheduler/promote docs only if the operator command changes.

### Acceptance Criteria

- Shadow run compares old production thresholds against pending/proposed thresholds, not identical dicts.
- A P1 explosion blocks promotion.
- Report includes `old`, `new`, `p1_delta`, `avg_score_delta`, `explosions`, and `safe_to_promote`.
- CLI behavior is deterministic with mocked/local data in tests.

### Verification

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
.\.venv\Scripts\python.exe -m pytest tests/test_cro_thresholds.py tests/test_cro_threshold_feedback.py tests/test_cro_thresholds_two_stage.py tests/test_cro_threshold_tuning.py tests/test_cro_threshold_seasonal.py -q
.\.venv\Scripts\python.exe scripts\cro_threshold_shadow.py
```

### Prompt For Next Conversation

```text
Continue CRO threshold shadow promotion in C:\Users\poonx\Dajian_Listing_Tool.

Read:
- docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md
- docs/CRO_SYSTEM.md
- scripts/cro_threshold_shadow.py
- scripts/cro_promote_thresholds.py
- src/services/cro_thresholds.py
- tests/test_cro_thresholds_two_stage.py

Objective:
Replace the placeholder new_thr = old_thr path with a real two-stage shadow gate that compares production thresholds to pending/proposed thresholds before promotion.

Constraints:
- Promotion must be blocked when P1 actions explode.
- Keep reports under reports/.
- Run the threshold-focused tests before finishing.
```

## Plan 4: Harden MI External Discovery And Auto-Draft Recovery

### Why This Is Still Open

MI is documented as a first-class subsystem and has automated tests. The
remaining work is production hardening around the external GigaCloud discovery
path and auto-draft recovery:

- `src/plugins/terapeak_research/external_discovery.py` intentionally avoids Qwen and only moves opportunities to `PENDING`.
- Operators still need clear diagnostics when the live GigaCloud/Dajian/Terapeak path returns no candidates, no prices, no market data, or missing credentials.
- Manual live probes exist under `tools/`, but deterministic tests should capture stable failure modes.

### Read These Files

- `src/plugins/terapeak_research/external_discovery.py`
- `src/plugins/terapeak_research/intelligence_service.py`
- `src/plugins/terapeak_research/history.py`
- `src/plugins/terapeak_research/scoring.py`
- `src/web/pages/market_intelligence.py`
- `daily_tasks.py`
- `scripts/mi_diagnose.py`
- `tests/test_mi_external_discovery.py`
- `tests/test_market_intelligence_scoring.py`
- `tests/test_mi_e2e_smoke.py`
- `tests/test_mi_draft_origin.py`
- `tests/test_scheduler_mi_self_check.py`
- `tests/test_mi_diagnose.py`
- `docs/ARCHITECTURE.md`
- `docs/USAGE.md`

### Development Steps

1. Verify `discover_external_opportunities(..., return_diagnostics=True)` surfaces every drop reason needed by operators.
2. Ensure Streamlit MI page exposes external discovery diagnostics without spending Qwen tokens.
3. Verify `ingest_external_opportunity_as_pending()` preserves live `PUBLISHED` / `READY` rows and correctly reactivates `ENDED` / `DELISTED`.
4. Add deterministic tests for any observed no-candidate or credential-missing behavior.
5. If diagnostics change, update `scripts/mi_diagnose.py` and docs.

### Acceptance Criteria

- External discovery never recommends currently `PUBLISHED` or `READY` SKUs.
- `ENDED` / `DELISTED` can be reactivated as `PENDING` with an MI log marker.
- Missing credentials produce a clean diagnostic, not a confusing empty success.
- No Qwen call is made during external discovery.

### Verification

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
.\.venv\Scripts\python.exe -m pytest tests/test_mi_external_discovery.py tests/test_market_intelligence_scoring.py tests/test_mi_e2e_smoke.py tests/test_mi_draft_origin.py tests/test_scheduler_mi_self_check.py tests/test_mi_diagnose.py -q
.\.venv\Scripts\python.exe scripts\mi_diagnose.py --json
```

Use live probes only after deterministic tests pass:

```powershell
.\.venv\Scripts\python.exe tools\probe_dajian_api.py
.\.venv\Scripts\python.exe tools\probe_terapeak.py
```

### Prompt For Next Conversation

```text
Continue MI external discovery hardening in C:\Users\poonx\Dajian_Listing_Tool.

Read:
- docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md
- docs/USAGE.md
- src/plugins/terapeak_research/external_discovery.py
- src/plugins/terapeak_research/intelligence_service.py
- src/web/pages/market_intelligence.py
- tests/test_mi_external_discovery.py
- scripts/mi_diagnose.py

Objective:
Make the external GigaCloud discovery and auto-draft recovery path operator-clear and deterministic: diagnostics for empty results, no Qwen token spend, safe PENDING ingest, and UI/diagnose visibility.

Constraints:
- Do not recommend PUBLISHED or READY SKUs.
- Do not call Qwen during external discovery.
- Run the MI regression slice before finishing.
```

## Plan 5: Productionize CRO Ops Snapshot And UI Drill-Down

### Why This Is Still Open

The S131-S140 CRO governance layer is implemented and documented, but much of
it is advisory and service-layer only. The next useful development work is to
make the ops snapshot actionable for operators without letting it mutate live
listings.

### Read These Files

- `docs/CRO_SYSTEM.md`
- `src/services/cro_ops_control_plane.py`
- `scripts/cro_ops_snapshot.py`
- `src/web/pages/cro_status.py`
- `src/services/cro_capacity_planning.py`
- `src/services/cro_disaster_recovery.py`
- `src/services/cro_compliance_audit.py`
- `src/services/cro_continuous_improvement.py`
- `tests/test_cro_ops_control_plane.py`
- `tests/test_scheduler_cro_ops.py`
- `tests/test_cro_capacity_planning.py`
- `tests/test_cro_disaster_recovery.py`
- `tests/test_cro_compliance_audit.py`
- `tests/test_cro_continuous_improvement.py`

### Development Steps

1. Run `scripts/cro_ops_snapshot.py` locally and inspect `logs/cro_ops_snapshot.json`.
2. Compare the snapshot schema with what `src/web/pages/cro_status.py` displays.
3. Add missing UI drill-downs for:
   - capacity severity
   - DR drill status
   - compliance log status
   - weekly improvement brief
4. Keep the page read-only. Do not add publish/reprice/delist controls here.
5. Add tests around snapshot rendering helpers or schema transforms where possible.

### Acceptance Criteria

- `python scheduler_daemon.py --task cro_ops` produces a readable snapshot.
- `src/web/pages/cro_status.py` can display key sections even when one optional artifact is missing.
- UI remains observation/advisory only.
- Snapshot schema changes are covered by tests.

### Verification

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
.\.venv\Scripts\python.exe -m pytest tests/test_cro_ops_control_plane.py tests/test_scheduler_cro_ops.py tests/test_cro_capacity_planning.py tests/test_cro_disaster_recovery.py tests/test_cro_compliance_audit.py tests/test_cro_continuous_improvement.py -q
.\.venv\Scripts\python.exe scripts\cro_ops_snapshot.py --brief logs\cro_weekly_improvement.md
.\.venv\Scripts\python.exe scheduler_daemon.py --task cro_ops
```

### Prompt For Next Conversation

```text
Continue CRO ops snapshot productionization in C:\Users\poonx\Dajian_Listing_Tool.

Read:
- docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md
- docs/CRO_SYSTEM.md
- src/services/cro_ops_control_plane.py
- scripts/cro_ops_snapshot.py
- src/web/pages/cro_status.py
- tests/test_cro_ops_control_plane.py
- tests/test_scheduler_cro_ops.py

Objective:
Make the CRO ops snapshot and CRO status page more useful for operators while keeping it strictly read-only/advisory.

Constraints:
- The ops page must not publish, delist, reprice, or promote.
- Missing optional logs should degrade gracefully.
- Run the S131-S140 focused tests before finishing.
```

## Plan 6: Continue Root Structure Cleanup Safely

### Why This Is Still Open

Root cleanup has an audit tool and documentation, but `docs/USAGE.md` says
future migration must confirm callers one by one before moving files. The next
step is not bulk moving; it is generating a current report and turning review
candidates into small migration commits.

### Read These Files

- `tools/root_structure_audit.py`
- `tests/test_root_structure_audit.py`
- `docs/REPO_HYGIENE_PLAN.md`
- `docs/REPO_HYGIENE_AUDIT.md`
- `docs/SCRIPT_MATRIX.md`
- `docs/USAGE.md`
- `.gitignore`

### Development Steps

1. Generate a fresh root audit report.
2. Review every non-keep candidate and classify it as:
   - active root entrypoint
   - runtime/cache/log artifact
   - maintained script that belongs in `scripts/`
   - local diagnostic that belongs in `tools/local_diagnostics/`
   - data/report artifact that belongs in `reports/`, `logs/`, or `.gitignore`
3. For each move, search callers first with `rg`.
4. Add compatibility shims only when a known caller still expects the old path.
5. Update docs and tests in the same commit as each migration slice.

### Acceptance Criteria

- No root file is moved without caller search evidence.
- `tools/root_structure_audit.py` reports fewer review candidates.
- Moved scripts still run from repo root.
- Runtime artifacts are ignored or routed to `cache/`, `logs/`, or `reports/`.

### Verification

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
.\.venv\Scripts\python.exe tools\root_structure_audit.py --output reports\root_structure_audit.json --markdown reports\root_structure_audit.md
.\.venv\Scripts\python.exe -m pytest tests/test_root_structure_audit.py -q
```

### Prompt For Next Conversation

```text
Continue root structure cleanup in C:\Users\poonx\Dajian_Listing_Tool.

Read:
- docs/NEXT_DEVELOPMENT_HANDOFF_PLANS.md
- docs/REPO_HYGIENE_PLAN.md
- docs/REPO_HYGIENE_AUDIT.md
- docs/SCRIPT_MATRIX.md
- tools/root_structure_audit.py
- tests/test_root_structure_audit.py

Objective:
Generate a fresh root audit and migrate only clearly classified root clutter in small, caller-verified commits.

Constraints:
- Do not bulk move files.
- Search callers with rg before moving anything.
- Preserve active root entrypoints.
- Run tests/test_root_structure_audit.py after changes.
```

## Maintenance: Line-Ending Normalization (dedicated cleanup commit)

The working tree shows ~490 dirty files, but ~479 are pure CRLF flips (repo canon
is LF; an editor re-saved files as CRLF). Only ~10 are real content changes. A root
`.gitattributes` (LF canon; `.bat`/`.cmd`/`.ps1` keep CRLF) is already written and
untracked. This is a ONE-TIME cleanup; do it as its own commit, never folded into a
feature commit.

Cleanest timing: run it when the tree has no uncommitted content work you care about
(e.g. right after Plan 1 is committed). Then step 2 below is empty and it is one shot.

Safety gate: `git diff --cached --ignore-cr-at-eol` MUST be empty before committing —
empty proves the staged change is line-endings only and no content (e.g. Plan 1 WIP)
was swept in.

PowerShell, in `C:\Users\poonx\Dajian_Listing_Tool`:

```powershell
# 0. confirm branch
git status --short --branch

# 1. disable autocrlf, stage .gitattributes, renormalize whole tree to LF
git config core.autocrlf false
git add .gitattributes
git add --renormalize .

# 2. if any REAL content change got staged (e.g. Plan 1 WIP), it prints here.
#    Unstage each listed file so this commit stays line-endings-only:
git diff --cached --ignore-cr-at-eol --stat
#    for each printed file:  git restore --staged <path>

# 3. SAFETY GATE — this must output nothing before you commit:
git diff --cached --ignore-cr-at-eol

# 4. commit (line-ending normalization only)
git commit -m "chore: normalize line endings to LF"

# 5. confirm the ~10 in-progress files are still in the working tree (not lost)
git status --short
```

## Suggested Execution Order

1. Finish Plan 1 first because it already has dirty worktree changes.
2. Then choose either Plan 2 or Plan 3 depending on CRO priority.
3. Run Plan 4 when operators need better MI discovery diagnostics.
4. Run Plan 5 when CRO ops visibility is more important than new mutation paths.
5. Run Plan 6 only in small cleanup commits after feature work is stable.

## Global Verification Before Handoff Back

For any completed plan:

```powershell
git status --short --branch
git diff --staged --name-status
```

For broad shared behavior changes:

```powershell
$env:PYTHONPATH='C:\Users\poonx\Dajian_Listing_Tool'
.\.venv\Scripts\python.exe -m pytest -q
```

Do not claim the repository is clean unless `git status --short --branch`
actually shows no modified, untracked, or staged files.
