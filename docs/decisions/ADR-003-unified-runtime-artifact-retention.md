# ADR-003: Use a Conservative Unified Retention Policy for Runtime Artifacts

## Status

Accepted

## Date

2026-08-12

## Context

The repository writes reports and logs from several independent flows. Email
reports, MI snapshots, Terapeak reports, CRO reports, audit outputs, and task
logs previously used separate or missing cleanup rules. This caused old
runtime artifacts to accumulate while making broad deletion unsafe because
the same directories also contain state files, evidence, credentials, and
debug assets.

## Decision

1. Centralize cleanup rules in `src/utils/report_retention.py`.
2. Match explicitly whitelisted filename families with parseable dates, plus a
   safe fallback for dated ordinary text artifacts.
3. Use calendar dates from filenames rather than filesystem mtime.
4. Retain email-side reports for 3 days, MI snapshots for 30 days, ordinary
   reports/logs for 30 days, and critical audit/remediation/recovery evidence
   for 180 days.
5. Never automatically delete undated files, state files, locks, databases,
   secrets, unsupported extensions, directory symlink targets, or symlink
   files. An unregistered but dated ordinary text artifact may use the
   30-day safe fallback; it is not treated as a protected state artifact.
6. Run cleanup from the daily task, while keeping a separate dry-run/apply CLI
   for inspection and manual operations.
7. Cleanup errors are logged and do not cause the business task to rerun.

## Alternatives Considered

### Delete everything older than N days in `reports/` and `logs/`

Rejected: the directories contain state, credentials, recovery evidence, and
unknown artifacts that do not have a uniform lifecycle.

### Keep each module's local cleanup implementation

Rejected: retention periods and date semantics drift, and new report families
are easy to forget.

### Use filesystem mtime for all cleanup decisions

Rejected: copying or restoring a file can change mtime without changing the
artifact's report date.

## Consequences

- Runtime directories stop growing indefinitely for known report/log families.
- Critical evidence remains available for six months.
- Undated, state-like, binary, and otherwise unsupported artifacts require
  explicit review; dated ordinary text artifacts use the safe fallback.
- The daily task performs extra local filesystem work, but cleanup is
  bounded to two directories and never calls external services.
