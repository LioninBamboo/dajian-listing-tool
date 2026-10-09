"""Conservative retention policy for runtime reports and logs.

Explicitly named artifact families use their dedicated retention tiers. A
safe fallback also covers dated, ordinary text artifacts so new producers do
not silently accumulate files. Files with missing dates, state-like names,
unsupported extensions, or symlink targets are left untouched by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
import re
from typing import Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]

EMAIL_REPORT_PATTERNS = (
    "daily_report_*.html",
    "mi_digest_*.html",
    "health_check_*.json",
    "reprice_report_*.json",
    "reprice_changes_*.html",
    "reprice_changes_*.csv",
)

SAFE_FALLBACK_SUFFIXES = {
    ".csv",
    ".err",
    ".html",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".out",
    ".txt",
    ".xml",
}

_FALLBACK_RULE_NAMES = {"dated_reports_fallback", "dated_logs_fallback"}
_PROTECTED_NAME_RE = re.compile(
    r"(^_|(?:^|_)(?:state|history|jobs?|scheduler|watchdog|lock|snapshot)(?:_|\.|$))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RetentionRule:
    name: str
    directory: str
    patterns: tuple[str, ...]
    keep_days: int


@dataclass(frozen=True)
class RetentionCandidate:
    path: Path
    rule_name: str
    keep_days: int
    artifact_date: date

    @property
    def relative_path(self) -> str:
        return str(self.path)


# More specific / safety-sensitive rules are ordered first. A path is assigned
# to its first matching rule so one artifact cannot be classified twice.
RETENTION_RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        "critical_reports",
        "reports",
        (
            "critical_*",
            "crit_*",
            "cro_*delist*",
            "cro_*relist*",
            "cro_*revive*",
            "cro_*verify*",
            "cro_phase3*",
            "cro_final*",
            "cro_true*",
            "image_restore_*",
            "placeholder_image_repair_*",
            "emergency_reprice_*",
            "under_floor_raise_*",
        ),
        180,
    ),
    RetentionRule("email_reports", "reports", EMAIL_REPORT_PATTERNS, 3),
    RetentionRule(
        "mi_snapshots",
        "reports",
        ("mi_opportunities_*.json",),
        30,
    ),
    RetentionRule(
        "operational_reports",
        "reports",
        (
            "terapeak_report_*",
            "cro_daily_*",
            "cro_threshold_shadow_*",
            "compare_*",
            "audit_*",
            "dev_daily_report_*",
            "giga_*",
            "cost_backfill_*",
        ),
        30,
    ),
    RetentionRule(
        "critical_logs",
        "logs",
        (
            "critical*",
            "crit*",
            "*audit*",
            "*repair*",
            "*recovery*",
            "*restore*",
            "*rollback*",
            "*delist*",
            "*relist*",
            "*postfix*",
            "*remediation*",
            "manual_full_audit*",
            "semantic_rewrite*",
        ),
        180,
    ),
    RetentionRule(
        "operational_logs",
        "logs",
        (
            "daily_*",
            "inventory_*",
            "batch_*",
            "publish_*",
            "ready_*",
            "source_*",
            "compare*",
            "cro_*",
            "ad_*",
            "bid_*",
            "auto_*",
            "reprice_*",
            "fastapi_*",
            "measurement_*",
            "description_*",
            "conversion_*",
            "category_*",
            "assembly_*",
            "brand_*",
            "qc_*",
            "active_*",
            "collected_*",
            "nonterminal_*",
            "priority_*",
            "fix_*",
            "supplier_*",
        ),
        30,
    ),
    # Script-specific prefixes above take precedence. This final rule catches
    # dated, ordinary text artifacts whose producer has not been registered
    # yet, while leaving state, snapshot, lock, binary, and image files alone.
    RetentionRule("dated_reports_fallback", "reports", ("*",), 30),
    RetentionRule("dated_logs_fallback", "logs", ("*",), 30),
)


_DATE_TOKEN_RE = re.compile(
    r"(?P<date>20\d{2}(?:-\d{2}){2}|20\d{6})"
    r"(?:[_-](?P<time>\d{4}|\d{6}))?"
)


def parse_artifact_date(filename: str) -> date | None:
    """Extract the first valid ISO/compact date token from a filename."""
    for match in _DATE_TOKEN_RE.finditer(filename):
        raw_date = match.group("date")
        try:
            parsed = datetime.strptime(
                raw_date,
                "%Y-%m-%d" if "-" in raw_date else "%Y%m%d",
            )
        except ValueError:
            continue

        raw_time = match.group("time")
        if raw_time:
            try:
                datetime.strptime(raw_time, "%H%M" if len(raw_time) == 4 else "%H%M%S")
            except ValueError:
                continue
        return parsed.date()
    return None


def _matches_rule(filename: str, rule: RetentionRule) -> bool:
    normalized = filename.lower()
    return any(fnmatchcase(normalized, pattern.lower()) for pattern in rule.patterns)


def _is_fallback_eligible(path: Path) -> bool:
    return (
        _is_supported_artifact(path)
        and not _PROTECTED_NAME_RE.search(path.name)
    )


def _is_supported_artifact(path: Path) -> bool:
    """Allow only text/report formats; protect binaries and runtime state."""
    return path.suffix.lower() in SAFE_FALLBACK_SUFFIXES


def _iter_rule_files(directory: Path, rule: RetentionRule) -> Iterable[Path]:
    if (
        not directory.exists()
        or not directory.is_dir()
        or directory.is_symlink()
    ):
        return
    for path in directory.iterdir():
        if (
            path.is_file()
            and not path.is_symlink()
            and _matches_rule(path.name, rule)
            and _is_supported_artifact(path)
            and (
                rule.name not in _FALLBACK_RULE_NAMES
                or _is_fallback_eligible(path)
            )
        ):
            yield path


def _is_expired(artifact_date: date, keep_days: int, now: datetime) -> bool:
    # Date-only filenames represent a whole calendar day. Keep the cutoff day
    # so a 3-day policy does not delete that day's report halfway through it.
    cutoff_date = (now - timedelta(days=keep_days)).date()
    return artifact_date < cutoff_date


def _candidate_from_path(
    path: Path,
    rule: RetentionRule,
    now: datetime,
) -> RetentionCandidate | None:
    artifact_date = parse_artifact_date(path.name)
    if (
        not _is_supported_artifact(path)
        or _PROTECTED_NAME_RE.search(path.name)
        or artifact_date is None
        or not _is_expired(artifact_date, rule.keep_days, now)
    ):
        return None
    return RetentionCandidate(
        path=path,
        rule_name=rule.name,
        keep_days=rule.keep_days,
        artifact_date=artifact_date,
    )


def _iter_candidates(
    paths_and_rules: Iterable[tuple[Path, RetentionRule]],
    now: datetime,
) -> list[RetentionCandidate]:
    candidates: list[RetentionCandidate] = []
    seen: set[Path] = set()
    for path, rule in paths_and_rules:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidate = _candidate_from_path(path, rule, now)
        if candidate is not None:
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: str(item.path).lower())


def find_expired_artifacts(
    project_root: Path | None = None,
    now: datetime | None = None,
    rule_names: Sequence[str] | None = None,
) -> list[RetentionCandidate]:
    """Return expired, policy-eligible files without changing disk."""
    root = Path(project_root or PROJECT_ROOT).resolve()
    current_time = now or datetime.now()
    selected = set(rule_names) if rule_names is not None else None
    paths_and_rules: list[tuple[Path, RetentionRule]] = []

    for rule in RETENTION_RULES:
        if selected is not None and rule.name not in selected:
            continue
        directory = root / rule.directory
        paths_and_rules.extend(
            (path, rule) for path in _iter_rule_files(directory, rule)
        )

    return _iter_candidates(paths_and_rules, current_time)


def find_expired_named_artifacts(
    directory: Path,
    patterns: Sequence[str],
    keep_days: int,
    now: datetime | None = None,
) -> list[RetentionCandidate]:
    """Find expired files for a compatibility wrapper with custom patterns."""
    if keep_days < 0 or not directory.exists() or not directory.is_dir():
        return []
    current_time = now or datetime.now()
    rule = RetentionRule("custom", str(directory), tuple(patterns), keep_days)
    paths_and_rules = (
        (path, rule)
        for path in directory.iterdir()
        if (
            path.is_file()
            and not path.is_symlink()
            and _matches_rule(path.name, rule)
            and _is_supported_artifact(path)
            and not _PROTECTED_NAME_RE.search(path.name)
        )
    )
    return _iter_candidates(paths_and_rules, current_time)


def cleanup_runtime_artifacts(
    project_root: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = True,
) -> dict:
    """Find and optionally remove expired runtime artifacts.

    The default is intentionally dry-run. Callers must pass ``dry_run=False``
    (or use the CLI's explicit ``--apply`` flag) to delete files.
    """
    candidates = find_expired_artifacts(project_root=project_root, now=now)
    removed: list[str] = []
    errors: list[dict[str, str]] = []

    if not dry_run:
        for candidate in candidates:
            try:
                candidate.path.unlink()
                removed.append(str(candidate.path))
            except OSError as exc:
                errors.append({"path": str(candidate.path), "error": str(exc)})

    return {
        "dry_run": dry_run,
        "candidate_count": len(candidates),
        "removed_count": len(removed),
        "error_count": len(errors),
        "errors": errors,
        "candidates": [
            {
                "path": str(candidate.path),
                "rule": candidate.rule_name,
                "keep_days": candidate.keep_days,
                "artifact_date": candidate.artifact_date.isoformat(),
            }
            for candidate in candidates
        ],
        "removed": removed,
    }


def purge_named_artifacts(
    directory: Path,
    patterns: Sequence[str],
    keep_days: int,
    now: datetime | None = None,
) -> int:
    """Delete a narrowly scoped named family for legacy call sites."""
    candidates = find_expired_named_artifacts(directory, patterns, keep_days, now)
    removed = 0
    for candidate in candidates:
        try:
            candidate.path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
