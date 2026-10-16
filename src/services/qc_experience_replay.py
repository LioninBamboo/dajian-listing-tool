"""Read-only replay of the first QC experience/rule catalog."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.services.listing_publish_readback import verify_publish_readback
from src.services.qc_experience_registry import resolve_rule_provenance


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "ebay_collection.db"
DEFAULT_HISTORICAL_PATHS = (
    PROJECT_ROOT / "logs/critical_repair_dryrun_20260804.json",
    PROJECT_ROOT / "logs/critical_targeted_reaudit_20260804.json",
)
DEFAULT_CLEAN_PATHS = (
    PROJECT_ROOT / "logs/critical_targeted_reaudit_after_recommended_fixes_20260804.json",
)


REPLAY_CASES = (
    {
        "case_id": "REPLAY-CAPACITY-20260804",
        "experience_id": "EXP-CAPACITY-20260804",
        "rule_id": "FACT_SHEET-SEMANTIC_CAPACITY",
        "mode": "historical_issue",
        "issue_types": ("semantic_capacity",),
    },
    {
        "case_id": "REPLAY-HALL-TREE-20260804",
        "experience_id": "EXP-HALL-TREE-DIMENSIONS-20260804",
        "rule_id": "QUALITY_GATE-HALL_TREE_DIMENSION_ORIENTATION",
        "mode": "historical_issue",
        "issue_types": ("semantic_dimension", "wrong_dimension"),
    },
    {
        "case_id": "REPLAY-SIDE-TABLE-20260804",
        "experience_id": "EXP-SIDE-TABLE-CATEGORY-20260804",
        "rule_id": "QUALITY_GATE-SIDE_TABLE_CATEGORY_PROFILE",
        "mode": "historical_issue",
        "issue_types": ("category_mismatch",),
    },
    {
        "case_id": "REPLAY-INVENTORY-ARRIVAL-DATE-20260804",
        "experience_id": "EXP-INVENTORY-ARRIVAL-DATE-20260804",
        "rule_id": "INVENTORY-OOS-ARRIVAL-DATE-001",
        "mode": "policy_fixture",
        "fixture": {"giga_quantity": 0, "arrival_date": "2026-08-15", "ebay_quantity": 0},
    },
    {
        "case_id": "REPLAY-INVENTORY-STOCK-DRIFT-20260804",
        "experience_id": "EXP-INVENTORY-STOCK-RECONCILIATION-20260804",
        "rule_id": "INVENTORY-GIGA-STOCK-EBAY-QTY-001",
        "mode": "policy_fixture",
        "fixture": {"giga_quantity": 3, "arrival_date": None, "ebay_quantity": 0},
    },
    {
        "case_id": "REPLAY-PARTIAL-WRITE-20260804",
        "experience_id": "EXP-PARTIAL-WRITE-20260804",
        "rule_id": "PUBLISH-PARTIAL-WRITE-READBACK-001",
        "mode": "contract_fixture",
    },
)


def replay_inventory_policy(
    *,
    giga_quantity: int,
    arrival_date: str | None,
    ebay_quantity: int,
) -> dict[str, str]:
    """Replay inventory policy without calling either external system."""

    if int(giga_quantity) <= 0 and str(arrival_date or "").strip():
        return {
            "status": "manual_review",
            "action": "preserve_listing",
            "reason": "giga_oos_with_arrival_date",
        }
    if int(giga_quantity) > 0 and int(ebay_quantity) <= 0:
        return {
            "status": "manual_review",
            "action": "hold_end_and_reconcile",
            "reason": "giga_stock_ebay_zero",
        }
    return {
        "status": "no_trigger",
        "action": "no_policy_action",
        "reason": "no_inventory_policy_conflict",
    }


def _publish_expected() -> dict[str, Any]:
    return {
        "sku": "REPLAY-SKU",
        "title": "Solid Wood Side Table",
        "description": "<div>side table</div>",
        "image_urls": ["img-1", "img-2"],
        "video_ids": ["video-1"],
        "quantity": 2,
        "condition": "NEW",
        "package_weight_and_size": {"weight": {"value": 10, "unit": "POUND"}},
        "category_id": "38204",
        "offer_id": "offer-1",
        "listing_id": "listing-1",
    }


def _publish_inventory(image_urls: list[str]) -> dict[str, Any]:
    return {
        "condition": "NEW",
        "availability": {"shipToLocationAvailability": {"quantity": 2}},
        "product": {
            "title": "Solid Wood Side Table",
            "description": "<div>side table</div>",
            "imageUrls": image_urls,
            "videoIds": ["video-1"],
            "packageWeightAndSize": {"weight": {"value": 10, "unit": "POUND"}},
        },
    }


def _publish_offer() -> dict[str, Any]:
    return {
        "offerId": "offer-1",
        "categoryId": "38204",
        "status": "PUBLISHED",
        "listing": {"listingId": "listing-1", "listingStatus": "ACTIVE"},
    }


def replay_partial_write_fixture() -> dict[str, Any]:
    """Replay an API-error-shaped partial write using readback only."""

    partial = verify_publish_readback(
        _publish_expected(),
        _publish_inventory(["img-1"]),
        _publish_offer(),
    )
    clean = verify_publish_readback(
        _publish_expected(),
        _publish_inventory(["img-1", "img-2"]),
        _publish_offer(),
    )
    return {
        "status": partial.get("status"),
        "passed": bool(partial.get("passed")),
        "issue_codes": [item.get("code") for item in partial.get("issues") or []],
        "clean_control_status": clean.get("status"),
    }


def _read_audit_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("issues", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _issue_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("issues", "critical"):
        value = row.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _find_issue_matches(
    paths: list[Path],
    issue_types: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[str]]:
    matches: list[dict[str, Any]] = []
    missing: list[str] = []
    wanted = set(issue_types)
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        for row in _read_audit_rows(path):
            matched = [issue for issue in _issue_rows(row) if issue.get("type") in wanted]
            if matched:
                matches.append(
                    {
                        "source": str(path),
                        "sku": row.get("sku"),
                        "issue_types": sorted({str(issue.get("type")) for issue in matched}),
                    }
                )
    return matches, missing


def _count_issue_matches(
    paths: list[Path],
    issue_types: tuple[str, ...],
) -> tuple[int, int, list[str]]:
    issue_count = 0
    skus: set[str] = set()
    missing: list[str] = []
    wanted = set(issue_types)
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        for row in _read_audit_rows(path):
            matched = [issue for issue in _issue_rows(row) if issue.get("type") in wanted]
            issue_count += len(matched)
            if matched and row.get("sku"):
                skus.add(str(row["sku"]))
    return issue_count, len(skus), missing


def _resolve_provenance(db_path: Path, rule_ids: list[str]) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        return resolve_rule_provenance(connection, rule_ids)
    finally:
        connection.close()


def build_historical_replay_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    historical_paths: list[str | Path] | None = None,
    clean_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Build a read-only replay report for registry cases and clean controls."""

    database = Path(db_path).resolve()
    history = [Path(path).resolve() for path in (historical_paths or DEFAULT_HISTORICAL_PATHS)]
    clean = [Path(path).resolve() for path in (clean_paths or DEFAULT_CLEAN_PATHS)]
    rule_ids = [str(case["rule_id"]) for case in REPLAY_CASES]
    provenance = _resolve_provenance(database, rule_ids)
    provenance_by_rule = {item["rule_id"]: item for item in provenance}
    cases: list[dict[str, Any]] = []

    for case in REPLAY_CASES:
        rule_id = str(case["rule_id"])
        rule = provenance_by_rule.get(rule_id)
        mode = str(case["mode"])
        common = {
            "case_id": case["case_id"],
            "experience_id": case["experience_id"],
            "rule_id": rule_id,
            "rule_status": rule.get("status") if rule else None,
            "test_path": rule.get("test_path") if rule else None,
            "evidence_mode": mode,
            "rule_registered": bool(rule),
        }
        if mode == "historical_issue":
            matches, missing_history = _find_issue_matches(history, case["issue_types"])
            clean_matches, missing_clean = _find_issue_matches(clean, case["issue_types"])
            common.update(
                {
                    "replay_status": "blocked" if matches and rule else "not_observed",
                    "historical_match_count": len(matches),
                    "clean_control_match_count": len(clean_matches),
                    "historical_matches": matches,
                    "missing_historical_paths": missing_history,
                    "missing_clean_paths": missing_clean,
                    "historical_evidence_found": bool(matches),
                    "verified": bool(matches and rule and not clean_matches),
                }
            )
        elif mode == "policy_fixture":
            evaluation = replay_inventory_policy(**case["fixture"])
            common.update(
                {
                    "replay_status": evaluation["status"],
                    "policy_action": evaluation["action"],
                    "policy_reason": evaluation["reason"],
                    "historical_match_count": 0,
                    "clean_control_match_count": 0,
                    "historical_evidence_found": False,
                    "verified": bool(rule and evaluation["status"] == "manual_review"),
                }
            )
        else:
            replay = replay_partial_write_fixture()
            common.update(
                {
                    "replay_status": replay["status"],
                    "issue_codes": replay["issue_codes"],
                    "clean_control_status": replay["clean_control_status"],
                    "historical_match_count": 0,
                    "clean_control_match_count": 0,
                    "historical_evidence_found": False,
                    "verified": bool(
                        rule
                        and replay["status"] == "partial_write"
                        and replay["passed"] is False
                        and replay["clean_control_status"] == "verified"
                    ),
                }
            )
        cases.append(common)

    return {
        "db_path": str(database),
        "historical_paths": [str(path) for path in history],
        "clean_paths": [str(path) for path in clean],
        "rule_provenance": provenance,
        "cases": cases,
        "summary": {
            "case_count": len(cases),
            "verified_cases": sum(1 for case in cases if case["verified"]),
            "historical_evidence_cases": sum(
                1 for case in cases if case["historical_evidence_found"]
            ),
            "fixture_only_cases": sum(
                1 for case in cases if not case["historical_evidence_found"]
            ),
            "rule_provenance_complete": len(provenance) == len(rule_ids),
            "missing_rule_ids": [rule_id for rule_id in rule_ids if rule_id not in provenance_by_rule],
            "clean_control_matches": sum(case["clean_control_match_count"] for case in cases),
        },
    }


def build_experience_recurrence_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    historical_paths: list[str | Path] | None = None,
    clean_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Summarize recurrence and evidence gaps without changing QC state."""

    database = Path(db_path).resolve()
    history = [Path(path).resolve() for path in (historical_paths or DEFAULT_HISTORICAL_PATHS)]
    clean = [Path(path).resolve() for path in (clean_paths or DEFAULT_CLEAN_PATHS)]
    rule_ids = [str(case["rule_id"]) for case in REPLAY_CASES]
    provenance = _resolve_provenance(database, rule_ids)
    provenance_by_rule = {item["rule_id"]: item for item in provenance}
    rows: list[dict[str, Any]] = []

    for case in REPLAY_CASES:
        mode = str(case["mode"])
        if mode == "historical_issue":
            historical_issue_count, historical_sku_count, missing_history = _count_issue_matches(
                history, case["issue_types"]
            )
            clean_issue_count, clean_sku_count, missing_clean = _count_issue_matches(
                clean, case["issue_types"]
            )
        else:
            historical_issue_count = historical_sku_count = 0
            clean_issue_count = clean_sku_count = 0
            missing_history = []
            missing_clean = []

        rule = provenance_by_rule.get(str(case["rule_id"]))
        if clean_issue_count:
            triage = "investigate_recurrence"
        elif mode != "historical_issue":
            triage = "collect_cross_system_evidence"
        elif rule and rule.get("status") == "manual_review":
            triage = "keep_manual_review"
        elif rule and rule.get("status") == "enforced":
            triage = "keep_enforced"
        else:
            triage = "review_rule_status"

        rows.append(
            {
                "case_id": case["case_id"],
                "experience_id": case["experience_id"],
                "rule_id": case["rule_id"],
                "rule_status": rule.get("status") if rule else None,
                "historical_issue_count": historical_issue_count,
                "historical_sku_count": historical_sku_count,
                "clean_control_issue_count": clean_issue_count,
                "clean_control_sku_count": clean_sku_count,
                "evidence_gap": mode != "historical_issue",
                "missing_historical_paths": missing_history,
                "missing_clean_paths": missing_clean,
                "triage": triage,
            }
        )

    return {
        "db_path": str(database),
        "historical_paths": [str(path) for path in history],
        "clean_paths": [str(path) for path in clean],
        "rule_provenance": provenance,
        "rules": rows,
        "summary": {
            "rule_count": len(rows),
            "rules_with_historical_evidence": sum(
                1 for row in rows if row["historical_issue_count"] > 0
            ),
            "rules_with_clean_control_recurrence": sum(
                1 for row in rows if row["clean_control_issue_count"] > 0
            ),
            "rules_with_evidence_gap": sum(1 for row in rows if row["evidence_gap"]),
            "rule_provenance_complete": len(provenance) == len(rule_ids),
            "missing_rule_ids": [rule_id for rule_id in rule_ids if rule_id not in provenance_by_rule],
        },
    }
