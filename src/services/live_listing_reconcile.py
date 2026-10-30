"""Reconcile local collected_products status/listing_id to eBay live state.

Read-only by default. ``--apply`` writes only ``status`` and ``listing_id``.
Never ends, relists, or revises anything on eBay. Category-approval locks
(``optimization.categoryApproved`` / ``categoryApprovedId``) are left intact.
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from src.services.live_listing_coverage import (
    CoverageReport,
    DbListingRow,
    LiveListing,
    build_coverage_report,
    load_db_listing_rows,
)

logger = logging.getLogger(__name__)

ACTION_RESTORE_PUBLISHED = "restore_published"
ACTION_FIX_LISTING_ID = "fix_listing_id"
ACTION_MARK_ENDED = "mark_ended"
ACTION_ORPHAN = "orphan_unmapped"


@dataclass
class ReconcileChange:
    sku: str
    action: str
    reason: str
    item_id: str = ""
    db_status_before: str = ""
    db_status_after: str = ""
    db_listing_id_before: str = ""
    db_listing_id_after: str = ""
    match_by: str = ""
    category_lock: bool = False
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReconcileReport:
    generated_at: str
    fetch_complete: bool
    apply: bool
    live_count: int
    changes: list[ReconcileChange] = field(default_factory=list)
    orphans: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "fetch_complete": self.fetch_complete,
            "apply": self.apply,
            "live_count": self.live_count,
            "change_count": len(self.changes),
            "orphan_count": len(self.orphans),
            "applied_count": sum(1 for row in self.changes if row.applied),
            "changes": [row.as_dict() for row in self.changes],
            "orphans": self.orphans,
        }


def plan_reconcile(report: CoverageReport) -> list[ReconcileChange]:
    """Build local status/listing_id repairs from a live coverage snapshot."""
    changes: list[ReconcileChange] = []
    for match in report.covered:
        if match.status_drifted:
            changes.append(ReconcileChange(
                sku=match.sku,
                action=ACTION_RESTORE_PUBLISHED,
                reason=f"live on eBay ({match.match_by}) but DB status={match.db_status or '-'}",
                item_id=match.live_item_id,
                db_status_before=match.db_status,
                db_status_after="PUBLISHED",
                db_listing_id_before=match.db_listing_id,
                db_listing_id_after=match.live_item_id or match.db_listing_id,
                match_by=match.match_by,
                category_lock=match.category_lock,
            ))
        elif match.listing_id_drifted:
            changes.append(ReconcileChange(
                sku=match.sku,
                action=ACTION_FIX_LISTING_ID,
                reason="live item id differs from stored listing_id",
                item_id=match.live_item_id,
                db_status_before=match.db_status,
                db_status_after=match.db_status or "PUBLISHED",
                db_listing_id_before=match.db_listing_id,
                db_listing_id_after=match.live_item_id,
                match_by=match.match_by,
                category_lock=match.category_lock,
            ))

    if report.fetch_complete:
        for row in report.published_not_live:
            changes.append(ReconcileChange(
                sku=row.sku,
                action=ACTION_MARK_ENDED,
                reason="DB PUBLISHED but listing is not in eBay ActiveList",
                item_id=row.listing_id,
                db_status_before=row.status,
                db_status_after="ENDED",
                db_listing_id_before=row.listing_id,
                db_listing_id_after=row.listing_id,
                match_by="",
                category_lock=row.category_lock,
            ))
    return changes


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(collected_products)")}


def _apply_status_listing_id(
    conn: sqlite3.Connection,
    sku: str,
    status: str,
    listing_id: str,
) -> None:
    columns = _table_columns(conn)
    assignments: list[str] = []
    values: list[Any] = []
    if "status" in columns:
        assignments.append("status = ?")
        values.append(status)
    if "listing_id" in columns:
        assignments.append("listing_id = ?")
        values.append(listing_id or None)
    if "updated_at" in columns:
        assignments.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
    if not assignments:
        return
    values.append(sku)
    conn.execute(
        f"UPDATE collected_products SET {', '.join(assignments)} WHERE sku = ?",
        tuple(values),
    )


def _confirmed_ended(
    change: ReconcileChange,
    ended_confirmer: Optional[Callable[[str, str], bool]],
) -> bool:
    sku = str(change.sku or "").strip()
    listing_id = str(change.item_id or change.db_listing_id_before or "").strip()
    if ended_confirmer is not None:
        try:
            return bool(ended_confirmer(sku, listing_id))
        except Exception as exc:
            logger.warning("ended confirmer failed for %s: %s", sku, exc)
            return False
    if not listing_id:
        return False
    try:
        from src.services.listing_status_sync import confirm_listing_ended
        return bool(confirm_listing_ended(listing_id))
    except Exception as exc:
        logger.warning("GetItem confirm-ended failed for %s: %s", listing_id, exc)
        return False


def apply_reconcile(
    db_path: str | Path,
    changes: Iterable[ReconcileChange],
    *,
    apply: bool = False,
    ended_confirmer: Optional[Callable[[str, str], bool]] = None,
) -> list[ReconcileChange]:
    """Write status/listing_id only. Never touches optimization / category locks."""
    applied: list[ReconcileChange] = []
    writable = [row for row in changes if row.action != ACTION_ORPHAN]
    if not apply:
        return list(writable)

    conn = sqlite3.connect(str(db_path))
    try:
        for change in writable:
            if not str(change.sku or "").strip():
                continue
            if change.action == ACTION_MARK_ENDED and not _confirmed_ended(
                change, ended_confirmer
            ):
                logger.warning(
                    "reconcile skip mark_ended sku=%s — eBay did not confirm Ended",
                    change.sku,
                )
                change.applied = False
                continue
            _apply_status_listing_id(
                conn,
                change.sku,
                change.db_status_after,
                change.db_listing_id_after,
            )
            change.applied = True
            applied.append(change)
            logger.info(
                "reconcile apply sku=%s action=%s status %s→%s listing_id %s→%s lock=%s",
                change.sku,
                change.action,
                change.db_status_before or "-",
                change.db_status_after or "-",
                change.db_listing_id_before or "-",
                change.db_listing_id_after or "-",
                change.category_lock,
            )
        conn.commit()
    finally:
        conn.close()
    return applied


def build_reconcile_report(
    db_path: str | Path,
    live_listings: Iterable[LiveListing],
    *,
    fetch_complete: bool = True,
    apply: bool = False,
    db_rows: Optional[list[DbListingRow]] = None,
    ended_confirmer: Optional[Callable[[str, str], bool]] = None,
) -> ReconcileReport:
    rows = db_rows if db_rows is not None else load_db_listing_rows(db_path)
    coverage = build_coverage_report(
        live_listings,
        rows,
        fetch_complete=fetch_complete,
    )
    changes = plan_reconcile(coverage)
    if apply:
        apply_reconcile(
            db_path,
            changes,
            apply=True,
            ended_confirmer=ended_confirmer,
        )
    orphans = [
        {
            "sku": item.sku or "",
            "item_id": item.item_id,
            "action": ACTION_ORPHAN,
            "reason": "live on eBay but missing from DB; not inventing a row",
        }
        for item in coverage.orphans
    ]
    return ReconcileReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        fetch_complete=fetch_complete,
        apply=apply,
        live_count=coverage.live_count,
        changes=changes,
        orphans=orphans,
    )


def write_reconcile_reports(report: ReconcileReport, dest: Path) -> tuple[Path, Path]:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    json_path = dest if dest.suffix.lower() == ".json" else dest.with_suffix(".json")
    csv_path = json_path.with_suffix(".csv")
    json_path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    fieldnames = [
        "sku",
        "action",
        "reason",
        "item_id",
        "db_status_before",
        "db_status_after",
        "db_listing_id_before",
        "db_listing_id_after",
        "match_by",
        "category_lock",
        "applied",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for change in report.changes:
            writer.writerow(change.as_dict())
        for orphan in report.orphans:
            writer.writerow({
                "sku": orphan.get("sku", ""),
                "action": orphan.get("action", ACTION_ORPHAN),
                "reason": orphan.get("reason", ""),
                "item_id": orphan.get("item_id", ""),
                "db_status_before": "",
                "db_status_after": "",
                "db_listing_id_before": "",
                "db_listing_id_after": "",
                "match_by": "",
                "category_lock": False,
                "applied": False,
            })
    return json_path, csv_path
