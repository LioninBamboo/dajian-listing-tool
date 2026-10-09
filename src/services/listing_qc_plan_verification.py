"""Pure verification helpers for the listing QC plan evidence chain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_allowlist(path: str | Path) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        sku = line.strip()
        if sku and not sku.startswith("#") and sku not in seen:
            seen.add(sku)
            values.append(sku)
    return values


def verify_publish_dry_run(allowlist: list[str], results: list[dict[str, Any]]) -> dict[str, Any]:
    expected = list(dict.fromkeys(allowlist))
    actual = [str(row.get("sku") or "") for row in results]
    actual_set = set(actual)
    expected_set = set(expected)
    structural_failures: list[dict[str, Any]] = []
    for row in results:
        sku = str(row.get("sku") or "")
        dimensions = row.get("dimensions") or {}
        missing_dimensions = [key for key in ("Item Length", "Item Width", "Item Height") if not dimensions.get(key)]
        if row.get("status") != "dry_run":
            structural_failures.append({"sku": sku, "reason": "status_not_dry_run", "status": row.get("status")})
        if missing_dimensions:
            structural_failures.append({"sku": sku, "reason": "missing_dimensions", "fields": missing_dimensions})
        if int(row.get("images") or 0) < 2:
            structural_failures.append({"sku": sku, "reason": "images_below_minimum", "images": row.get("images")})
        if row.get("compatibility_issues"):
            structural_failures.append({"sku": sku, "reason": "compatibility_issues", "issues": row.get("compatibility_issues")})
    return {
        "allowlist_count": len(expected),
        "result_count": len(results),
        "allowlist_unique": len(expected) == len(allowlist),
        "allowlist_minus_results": sorted(expected_set - actual_set),
        "results_minus_allowlist": sorted(actual_set - expected_set),
        "duplicate_result_skus": sorted({sku for sku in actual if actual.count(sku) > 1}),
        "structural_failures": structural_failures,
        "verified": (
            len(expected) == len(results)
            and expected_set == actual_set
            and len(actual) == len(actual_set)
            and not structural_failures
        ),
    }


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_plan_evidence_report(
    *,
    allowlist_path: str | Path,
    dry_run_path: str | Path,
    ready_audit_path: str | Path,
    critical_audit_path: str | Path,
    detect_only_path: str | Path,
    cross_system_path: str | Path | None = None,
) -> dict[str, Any]:
    allowlist = load_allowlist(allowlist_path)
    dry_run = _read_json(dry_run_path)
    if isinstance(dry_run, dict):
        dry_run = dry_run.get("results") or dry_run.get("items") or []
    ready = _read_json(ready_audit_path)
    critical = _read_json(critical_audit_path)
    detect = _read_json(detect_only_path)
    cross = _read_json(cross_system_path) if cross_system_path else None
    dry_run_check = verify_publish_dry_run(allowlist, dry_run)
    detect_summary = {
        "total_published": detect.get("total_published"),
        "total_with_issues": detect.get("total_with_issues"),
        "total_transport_failures": detect.get("total_transport_failures"),
        "severity_counts": detect.get("severity_counts") or {},
        "fixed_count": detect.get("fixed_count", 0),
    }
    followups: list[str] = []
    if detect_summary["severity_counts"].get("CRITICAL", 0):
        followups.append("live detect-only still has CRITICAL; no live fix was authorized in this run")
    if detect_summary["total_transport_failures"]:
        followups.append("live detect-only has transport/availability failures")
    cross_summary = (cross or {}).get("summary", {}) if isinstance(cross, dict) else {}
    if cross_summary.get("giga_in_stock_ebay_zero"):
        followups.append("GIGA in-stock/eBay-zero rows are held for inventory reconciliation")
    if cross_summary.get("unknown_evidence"):
        followups.append("cross-system rows still have unknown evidence")
    return {
        "report_version": "listing-qc-plan-evidence-v1",
        "artifacts": {
            "allowlist": str(allowlist_path),
            "dry_run": str(dry_run_path),
            "ready_audit": str(ready_audit_path),
            "critical_audit": str(critical_audit_path),
            "detect_only": str(detect_only_path),
            "cross_system": str(cross_system_path) if cross_system_path else None,
        },
        "ready_audit": {
            "timestamp": ready.get("timestamp"),
            "total_ready_like": ready.get("total_ready_like"),
            "qc_pass": sum(1 for row in ready.get("qc_results", []) if row.get("status") == "pass"),
            "qc_blocked": sum(1 for row in ready.get("qc_results", []) if row.get("status") != "pass"),
        },
        "historical_critical_reaudit": critical.get("summary") or {},
        "live_detect_only": detect_summary,
        "cross_system": cross_summary,
        "dry_run_verification": dry_run_check,
        "followups": followups,
        "status": "verified_with_followups" if dry_run_check["verified"] else "blocked",
    }

