# Documentation Audit

Last reviewed: 2026-06-17

This file tracks which active Markdown documents were synchronized in the May 2026 documentation passes and what must be updated together the next time the runtime shape changes.

## Summary

This review was not a cosmetic cleanup. It corrected real documentation drift around:

- the generated-listing quality gate
- READY and publish-time product-family blockers
- the scheduler topology
- the CRO runtime path and ops/governance closure layer
- the CRO ops snapshot CLI/scheduler/UI surface
- the root structure audit workflow
- the live listing audit report semantics
- the detect-only versus explicit-fix split for active-listing audit
- the MI runtime path
- the MI self-check and diagnose entrypoints
- the actual automated test surface for F31 / F32 / F33

The goal was to make `README.md`, operator docs, architecture docs, testing docs, decision records, and agent-facing docs describe the same current system.

## Current Status

| File | Audience | Status | What it now covers |
|------|----------|--------|--------------------|
| `README.md` | repo overview | current | current entrypoints, safe publish workflow, listing quality gate, MI overview, CRO ops snapshot, root audit command |
| `docs/LISTING_QUALITY_GATE.md` | operators / maintainers | current | generated-listing quality gate, product-family rules, measurement/image blockers, recovery commands |
| `docs/decisions/ADR-001-listing-quality-gate.md` | maintainers / agents | current | decision record for centralizing generated-listing safety before READY and publish |
| `docs/decisions/ADR-002-live-listing-audit-replaces-scheduled-title-optimization.md` | maintainers / agents | current | decision record for disabling scheduled title optimization and replacing it with live audit + repair |
| `docs/CRO_SYSTEM.md` | operators / maintainers | current | CRO runtime path, scheduler cadence, queue consumers, S131-S140 ops interfaces and snapshot surface |
| `docs/USAGE.md` | operators | current | startup, scheduler table, listing quality gate, MI runbook, CRO ops snapshot, root audit, troubleshooting |
| `docs/ARCHITECTURE.md` | maintainers | current | runtime topology, listing quality gate placement, scheduler split, CRO + MI pipeline, self-check, diagnose flow |
| `docs/TESTING.md` | maintainers | current | current `tests/` suite, listing-quality regression slice, MI regression slice, CRO regression slice, root audit test, manual probes |
| `agent.md` | coding agents | current | concise guardrails for publish safety, listing quality gate, MI rules, CRO rules, root audit, and diagnosis |
| `.github/copilot-instructions.md` | coding agents | current | repo bootstrap, quality gate, publish safety, scheduler and command quick reference |
| `skill.md` | maintainers / agents | current | long-form capability map with listing quality gate, MI stage, CRO stage, root audit, constraints, and commands |
| `docs/SCRIPT_MATRIX.md` | operators / maintainers | current | script ownership map including quality utilities, CRO ops snapshot and root audit |
| `docs/DAJIAN_API_REFERENCE.md` | API reference | current enough | specialized reference, not a workflow guide |
| `docs/SMART_HTML_TRUNCATION.md` | feature note | current enough | targeted note; update only when that feature changes |
| `docs/marketplace_insights_request_20260330.md` | support history | intentionally historical | keep as a dated permission request record |
| `.github/copilot-instructions.md` | agent bootstrap | current enough | repo bootstrap and guardrails |

## What Was Corrected In This Pass

### Listing Quality Gate

The docs now consistently state that:

- generated listing data is normalized through `src/utils/listing_quality_gate.py`
- the quality gate runs before drafts enter `READY`, during READY audit, and before dry-run/live publish
- product-family rules cover patio furniture sets, storage ottomans, indoor benches, dining chairs, coffee tables, dining tables, and sofas
- quality blockers include wrong product-family category, forbidden non-furniture item specifics, missing or placeholder dimensions, mojibake, description measurement mismatch, and insufficient images
- live image collapse is handled through `tools/scan_image_collapse.py` and `tools/restore_listing_images.py`
- the focused regression slice is `tests/test_listing_quality_gate.py`, `tests/test_collection_analysis_regressions.py`, and `tests/test_inventory_image_regressions.py`

### Scheduler And Entrypoints

The docs now consistently distinguish:

- `scheduler_daemon.py` as the long-running scheduler
- `daily_tasks.py` as the daily workload entrypoint
- `scripts/mi_diagnose.py` as the MI operator diagnostic entrypoint

The schedule references are now aligned with code, including:

- `10:05` MI self-check
- `09:40` ad restore
- `09:45` blacklist cleanup
- `09:50` guard anomaly
- `09:55` CRO effect-audit gate
- `10:00-10:30` CRO queue consume window
- `11:30` live listing audit as a detect-only `--live --email` pass, not an implicit full-corpus repair

### CRO Runtime And Governance

The docs now consistently state that:

- `daily_tasks.py` runs `run_cro_diagnose()` in the default daily path
- CRO execution is consumed later by scheduler jobs rather than inline in `09:30`
- `docs/CRO_SYSTEM.md` is the current fact source for the CRO subsystem
- S131-S140 are service-layer ops/governance helpers surfaced through `scripts/cro_ops_snapshot.py`, `scheduler_daemon.py --task cro_ops`, and the `🚦 CRO 状态` page
- the intended public contracts for S138-S140 are backup/restore, compliance access logging, and continuous-improvement advisory interfaces
- `20:00` health check

### Market Intelligence

The docs now consistently state that:

- MI runs through `daily_tasks.run_mi_snapshot()` inside the default full daily flow
- there is no standalone `--mi-only` CLI today
- MI artifacts live under `reports/` and health output lives in `logs/_scheduler_health.json`
- MI emails are Chinese and include thumbnails
- `scripts/mi_diagnose.py --json` is part of the supported operator surface

### Live Audit Semantics

The docs now consistently state that:

- `logs/listing_audit_fix_*.json -> total_with_issues` counts unique listing rows with at least one issue
- one listing row can carry multiple nested `issues[]`
- large live-audit counts must be broken down by `issues[].type` before deciding whether the problem is duplication, historical live debt, or a same-day publish regression
- the scheduled `11:30` audit reports and emails, but does not auto-fix the full live corpus
- `2026-06-17` is the current reference example: `512` affected listings, `532` issue instances, `497` historical live rows, and `15` same-day publishes re-flagged by stricter live-audit rules

### Testing

The docs now name the current MI regression slice explicitly:

- `tests/test_market_intelligence_scoring.py`
- `tests/test_mi_e2e_smoke.py`
- `tests/test_scheduler_mi_self_check.py`
- `tests/test_mi_diagnose.py`

## Cross-File Maintenance Rules

Update the following files together when any of these change:

1. default startup path or scheduler topology
2. READY publish safety rules
3. listing quality gate behavior, product-family rules, or item-specific blockers
4. image handling or live image verification rules
5. price verification behavior
6. MI schedule, MI state files, MI alert behavior, or MI diagnostics
7. default automated test entrypoint, listing-quality regression slice, or MI regression slice
8. names or locations of operator-facing scripts
9. CRO scheduler cadence, CRO queue consumers, or S131-S140 public interfaces
10. root cleanup rules or root-level script ownership
11. live active-audit count semantics or detect-only versus explicit-fix behavior

Update these files together for daily-operation changes:

- `README.md`
- `docs/LISTING_QUALITY_GATE.md`
- `docs/CRO_SYSTEM.md`
- `docs/USAGE.md`
- `agent.md`
- `docs/SCRIPT_MATRIX.md`

Update these files together for architecture or behavior-surface changes:

- `docs/ARCHITECTURE.md`
- `docs/TESTING.md`
- `skill.md`
- `docs/decisions/` when the change is a durable design decision

If the change affects scheduler recovery knowledge or MI operational facts, update repo memory as well.

## Residual Notes

- `docs/SCRIPT_MATRIX.md` should be revisited whenever quality-gate helpers, publish scripts, or image recovery tools change ownership.
- Historical or support-request documents should stay historical. Do not flatten them into generic workflow docs.
- When a live-audit incident exposes a rule gap, update both operator docs and agent-facing docs in the same pass so future triage does not regress into “big number = duplicate rows” confusion.
