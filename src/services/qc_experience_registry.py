"""Registry for turning listing-fix experience into versioned QC rules.

The registry is intentionally additive and local.  It does not publish,
modify inventory, or change a QC decision by itself; it records the evidence
and lets a QC result point back to the rule/experience that produced it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


QC_EXPERIENCE_SCHEMA_VERSION = 1
EXPERIENCE_STATUSES = frozenset({"candidate", "confirmed", "enforced", "rejected", "deprecated"})
RULE_STATUSES = frozenset({"candidate", "shadow", "enforced", "manual_review", "deprecated"})
RULE_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", text):
        raise ValueError(f"{field} contains unsupported characters")
    return text


def _clean_status(value: Any, allowed: frozenset[str], field: str) -> str:
    status = str(value or "").strip().lower()
    if status not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return status


def _json(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"registry evidence is not JSON serializable: {exc}") from exc


def _decode(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def ensure_qc_experience_registry(conn, *, commit: bool = True) -> None:
    """Create the additive registry tables if they do not exist."""

    # Keep DDL inside the caller's transaction.  sqlite3.executescript()
    # implicitly commits a pending transaction before executing its script,
    # which would make a failed seed leave half-created registry tables.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_experiences (
            experience_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            domain TEXT NOT NULL,
            root_cause TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            fix_action TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            ruleset_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_rule_catalog (
            rule_id TEXT PRIMARY KEY,
            experience_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            severity TEXT NOT NULL,
            condition_text TEXT NOT NULL,
            action TEXT NOT NULL,
            source_evidence_required INTEGER NOT NULL DEFAULT 1,
            generator_path TEXT NOT NULL DEFAULT '',
            test_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            ruleset_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qc_rule_experience "
        "ON qc_rule_catalog (experience_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qc_rule_status "
        "ON qc_rule_catalog (status)"
    )
    if commit:
        conn.commit()


def register_experience(
    conn,
    *,
    experience_id: str,
    title: str,
    domain: str,
    root_cause: str,
    evidence: Any,
    fix_action: str,
    status: str = "candidate",
    metadata: Mapping[str, Any] | None = None,
    ruleset_version: str = "",
    commit: bool = True,
) -> dict[str, Any]:
    """Insert or update one experience case without changing live state."""

    ensure_qc_experience_registry(conn, commit=False)
    experience_id = _clean_identifier(experience_id, "experience_id")
    status = _clean_status(status, EXPERIENCE_STATUSES, "status")
    title = str(title or "").strip()
    domain = str(domain or "").strip()
    root_cause = str(root_cause or "").strip()
    fix_action = str(fix_action or "").strip()
    if not title or not domain or not root_cause or not fix_action:
        raise ValueError("title, domain, root_cause, and fix_action are required")

    now = _now()
    existing = conn.execute(
        "SELECT created_at FROM qc_experiences WHERE experience_id = ?",
        (experience_id,),
    ).fetchone()
    created_at = existing[0] if existing else now
    conn.execute(
        """
        INSERT INTO qc_experiences (
            experience_id, title, domain, root_cause, evidence_json,
            fix_action, status, metadata_json, ruleset_version,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(experience_id) DO UPDATE SET
            title=excluded.title,
            domain=excluded.domain,
            root_cause=excluded.root_cause,
            evidence_json=excluded.evidence_json,
            fix_action=excluded.fix_action,
            status=excluded.status,
            metadata_json=excluded.metadata_json,
            ruleset_version=excluded.ruleset_version,
            updated_at=excluded.updated_at
        """,
        (
            experience_id,
            title,
            domain,
            root_cause,
            _json(evidence),
            fix_action,
            status,
            _json(metadata or {}),
            str(ruleset_version or ""),
            created_at,
            now,
        ),
    )
    if commit:
        conn.commit()
    return {
        "experience_id": experience_id,
        "title": title,
        "domain": domain,
        "root_cause": root_cause,
        "evidence": evidence,
        "fix_action": fix_action,
        "status": status,
        "metadata": dict(metadata or {}),
        "ruleset_version": str(ruleset_version or ""),
        "created_at": created_at,
        "updated_at": now,
    }


def register_rule(
    conn,
    *,
    rule_id: str,
    experience_id: str,
    domain: str,
    severity: str,
    condition: str,
    action: str,
    source_evidence_required: bool = True,
    generator_path: str = "",
    test_path: str = "",
    status: str = "candidate",
    ruleset_version: str = "",
    commit: bool = True,
) -> dict[str, Any]:
    """Register a rule only when its source experience already exists."""

    ensure_qc_experience_registry(conn, commit=False)
    rule_id = _clean_identifier(rule_id, "rule_id")
    experience_id = _clean_identifier(experience_id, "experience_id")
    status = _clean_status(status, RULE_STATUSES, "status")
    severity = str(severity or "").strip().upper()
    if severity not in RULE_SEVERITIES:
        raise ValueError(f"severity must be one of: {', '.join(sorted(RULE_SEVERITIES))}")
    if not conn.execute(
        "SELECT 1 FROM qc_experiences WHERE experience_id = ?", (experience_id,)
    ).fetchone():
        raise ValueError(f"experience_id is not registered: {experience_id}")

    condition = str(condition or "").strip()
    action = str(action or "").strip()
    if not condition or not action:
        raise ValueError("condition and action are required")
    now = _now()
    existing = conn.execute(
        "SELECT created_at FROM qc_rule_catalog WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    created_at = existing[0] if existing else now
    conn.execute(
        """
        INSERT INTO qc_rule_catalog (
            rule_id, experience_id, domain, severity, condition_text,
            action, source_evidence_required, generator_path, test_path,
            status, ruleset_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rule_id) DO UPDATE SET
            experience_id=excluded.experience_id,
            domain=excluded.domain,
            severity=excluded.severity,
            condition_text=excluded.condition_text,
            action=excluded.action,
            source_evidence_required=excluded.source_evidence_required,
            generator_path=excluded.generator_path,
            test_path=excluded.test_path,
            status=excluded.status,
            ruleset_version=excluded.ruleset_version,
            updated_at=excluded.updated_at
        """,
        (
            rule_id,
            experience_id,
            domain,
            severity,
            condition,
            action,
            1 if source_evidence_required else 0,
            str(generator_path or ""),
            str(test_path or ""),
            status,
            str(ruleset_version or ""),
            created_at,
            now,
        ),
    )
    if commit:
        conn.commit()
    return {
        "rule_id": rule_id,
        "experience_id": experience_id,
        "domain": domain,
        "severity": severity,
        "condition": condition,
        "action": action,
        "source_evidence_required": bool(source_evidence_required),
        "generator_path": str(generator_path or ""),
        "test_path": str(test_path or ""),
        "status": status,
        "ruleset_version": str(ruleset_version or ""),
        "created_at": created_at,
        "updated_at": now,
    }


def resolve_rule_provenance(conn, rule_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Resolve registered rules in caller order; unknown rules remain absent."""

    ordered_ids = []
    seen = set()
    for value in rule_ids or []:
        rule_id = str(value or "").strip()
        if rule_id and rule_id not in seen:
            seen.add(rule_id)
            ordered_ids.append(rule_id)
    if not ordered_ids or not _table_exists(conn, "qc_rule_catalog"):
        return []

    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        """
        SELECT rule_id, experience_id, domain, severity, status, test_path
        FROM qc_rule_catalog
        WHERE rule_id IN (""" + placeholders + ")",
        ordered_ids,
    ).fetchall()
    by_id = {
        row[0]: {
            "rule_id": row[0],
            "experience_id": row[1],
            "domain": row[2],
            "severity": row[3],
            "status": row[4],
            "test_path": row[5],
        }
        for row in rows
    }
    return [by_id[rule_id] for rule_id in ordered_ids if rule_id in by_id]


def _rule_id(prefix: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return f"{prefix.upper()}-{normalized}" if normalized else None


def derive_rule_ids(
    *,
    quality_issues: Sequence[Mapping[str, Any]] | None = None,
    fact_sheet_violations: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Derive stable rule IDs from QC issue codes without persisting anything."""

    rule_ids: list[str] = []
    seen = set()
    for issue in quality_issues or []:
        rule_id = _rule_id("QUALITY_GATE", issue.get("code"))
        if rule_id and rule_id not in seen:
            seen.add(rule_id)
            rule_ids.append(rule_id)
    for violation in fact_sheet_violations or []:
        rule_id = _rule_id("FACT_SHEET", violation.get("claim_type"))
        if rule_id and rule_id not in seen:
            seen.add(rule_id)
            rule_ids.append(rule_id)
    return rule_ids


def derive_contextual_rule_ids(
    *,
    source_title: str = "",
    candidate_title: str = "",
    quality_issues: Sequence[Mapping[str, Any]] | None = None,
    fact_sheet_violations: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Add narrow product-family rules without broad SKU allowlists.

    Generic issue codes remain the primary signal.  These contextual IDs are
    only added when the source/candidate text identifies the product family,
    so a generic category or dimension issue is not attributed to a narrowly
    evidenced experience by accident.
    """

    text = " ".join(
        str(value or "").strip().lower()
        for value in (source_title, candidate_title)
        if str(value or "").strip()
    )
    quality_codes = {
        str(issue.get("code") or "").strip().lower()
        for issue in quality_issues or []
        if isinstance(issue, Mapping)
    }
    fact_types = {
        str(violation.get("claim_type") or "").strip().lower()
        for violation in fact_sheet_violations or []
        if isinstance(violation, Mapping)
    }

    rule_ids: list[str] = []
    if "hall tree" in text and (
        "semantic_dimension" in fact_types
        or "wrong_dimension" in quality_codes
        or "description_measurement_mismatch" in quality_codes
    ):
        rule_ids.append("QUALITY_GATE-HALL_TREE_DIMENSION_ORIENTATION")
    if ("side table" in text or "end table" in text) and (
        "category_mismatch" in quality_codes
        or "wrong_type_aspect" in quality_codes
        or "wrong_set_includes" in quality_codes
    ):
        rule_ids.append("QUALITY_GATE-SIDE_TABLE_CATEGORY_PROFILE")
    return rule_ids
