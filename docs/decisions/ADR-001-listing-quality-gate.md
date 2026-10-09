# ADR-001: Use a Shared Listing Quality Gate Before READY and Publish

## Status

Accepted

## Date

2026-05-11

## Context

The listing pipeline previously relied on several partially overlapping safeguards:

- AI-generated title, description, category, and item specifics from `qwen_optimizer.py`
- category matching in `src/services/ebay_category_matcher.py`
- measurement validation in `src/utils/publish_validation.py`
- READY repair in `scripts/audit_fix_ready_drafts.py`
- publish-time enrichment in `batch_publish.py`

This allowed the same class of mistake to appear in different places:

- patio furniture set drafts could become indoor sofa/table listings
- benches could become tables, stools, or pet furniture
- coffee tables could be recategorized as sofas because description copy mentioned sofa/couch context
- furniture drafts could carry shoe, sport, or pet item specifics
- chair-only sets could be written as `Dining Table & Chairs`
- long/width/height could be missing or placeholder text
- local multi-image listings could publish or revise with fewer live eBay images
- mojibake such as `Boucl谷` could enter titles, descriptions, or item specifics

The cost of these failures is high because they can reach live eBay listings and require manual repair.

## Decision

Introduce a shared listing quality gate in `src/utils/listing_quality_gate.py`.

The gate provides:

- deterministic product-family classification
- generated draft normalization
- forbidden aspect removal
- measurement synchronization into item specifics and description
- mojibake cleanup
- image-count blocking
- structured quality issues that can block READY and publish

The gate is called from:

- `batch_analyze.py`
- `daily_tasks.py`
- `server.py`
- `scripts/audit_fix_ready_drafts.py`
- `batch_publish.py`

`src/services/ebay_category_matcher.py` remains the taxonomy and plausibility layer. The quality gate does not replace eBay taxonomy logic; it constrains it with local product-family truth.

## Alternatives Considered

### Keep Rules Only in READY Audit

Pros:

- smaller change
- keeps generation code simple

Cons:

- bad drafts still enter `READY`
- UI or API paths can accidentally publish stale drafts if the audit is skipped
- product-family rules remain scattered

Rejected because publish safety must not depend on a single manual script call.

### Put More Rules Into the AI Prompt

Pros:

- may improve first draft quality
- fewer deterministic code changes

Cons:

- AI output is not a reliable safety boundary
- hard to regression test
- does not protect against historical drafts or taxonomy suggestion drift

Rejected because measurements, category safety, and image sufficiency need deterministic checks.

### Maintain Separate Rules Per Entrypoint

Pros:

- each entrypoint can be locally optimized

Cons:

- rules drift quickly
- bugs fixed in one path reappear in another
- tests become fragmented

Rejected because the observed failures came from inconsistent logic across paths.

## Consequences

- Listing generation and publish have a common safety layer.
- Future product-family rules should be added to `listing_quality_gate.py` and covered by tests.
- READY audit reports can surface `quality_gate` blockers in `logs/ready_draft_audit_*.json`.
- Publish dry-run now acts as a second quality gate rather than just an eBay payload preview.
- Documentation and agent guardrails must refer to the quality gate as the canonical place for generated-listing correctness.

## Verification

Initial verification for this decision:

```bash
python -m pytest tests/test_listing_quality_gate.py tests/test_collection_analysis_regressions.py tests/test_inventory_image_regressions.py
python -m py_compile src/utils/listing_quality_gate.py src/services/ebay_category_matcher.py batch_analyze.py daily_tasks.py server.py batch_publish.py scripts/audit_fix_ready_drafts.py
python scripts/audit_fix_ready_drafts.py
python batch_publish.py --dry-run
```

Expected outcomes:

- listing quality tests pass
- category/image regressions remain green
- READY audit reports `unresolved=0` before publish
- dry-run passes without live publishing
