"""Foundation for the CRO relist-then-delist lifecycle."""
from __future__ import annotations

import json
import logging
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"

logger = logging.getLogger(__name__)

ACTION_REVIVE_RELIST = "revive_relist"
ACTION_FINAL_DELIST = "final_delist"

STATUS_CANDIDATE = "candidate"
STATUS_APPROVED = "approved"
STATUS_PRECHECKED = "prechecked"
STATUS_OLD_WITHDRAWN = "old_withdrawn"
STATUS_READY_TO_PUBLISH = "ready_to_publish"
STATUS_PUBLISHED_NEW = "published_new"
STATUS_OBSERVING = "observing"
STATUS_REVIVED_SUCCESS = "revived_success"
STATUS_NEEDS_CONVERSION_HELP = "needs_conversion_help"
STATUS_FALLBACK_DELIST_PENDING = "fallback_delist_pending"
STATUS_FINAL_DELISTED = "final_delisted"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

STATUSES = {
    STATUS_CANDIDATE,
    STATUS_APPROVED,
    STATUS_PRECHECKED,
    STATUS_OLD_WITHDRAWN,
    STATUS_READY_TO_PUBLISH,
    STATUS_PUBLISHED_NEW,
    STATUS_OBSERVING,
    STATUS_REVIVED_SUCCESS,
    STATUS_NEEDS_CONVERSION_HELP,
    STATUS_FALLBACK_DELIST_PENDING,
    STATUS_FINAL_DELISTED,
    STATUS_SKIPPED,
    STATUS_FAILED,
}

ACTIVE_STATUSES = (
    STATUS_CANDIDATE,
    STATUS_APPROVED,
    STATUS_PRECHECKED,
    STATUS_OLD_WITHDRAWN,
    STATUS_READY_TO_PUBLISH,
    STATUS_PUBLISHED_NEW,
    STATUS_OBSERVING,
    STATUS_NEEDS_CONVERSION_HELP,
    STATUS_FALLBACK_DELIST_PENDING,
    STATUS_FAILED,
)

TERMINAL_STATUSES = {
    STATUS_REVIVED_SUCCESS,
    STATUS_FINAL_DELISTED,
    STATUS_SKIPPED,
}

ALLOWED_TRANSITIONS = {
    STATUS_CANDIDATE: {STATUS_APPROVED, STATUS_SKIPPED},
    STATUS_APPROVED: {STATUS_PRECHECKED, STATUS_SKIPPED},
    STATUS_PRECHECKED: {STATUS_OLD_WITHDRAWN, STATUS_SKIPPED},
    STATUS_OLD_WITHDRAWN: {STATUS_READY_TO_PUBLISH},
    STATUS_READY_TO_PUBLISH: {STATUS_PUBLISHED_NEW},
    STATUS_PUBLISHED_NEW: {STATUS_OBSERVING},
    STATUS_OBSERVING: {STATUS_REVIVED_SUCCESS, STATUS_NEEDS_CONVERSION_HELP, STATUS_FALLBACK_DELIST_PENDING},
    STATUS_NEEDS_CONVERSION_HELP: {STATUS_OBSERVING},
    STATUS_FALLBACK_DELIST_PENDING: {STATUS_FINAL_DELISTED},
    STATUS_REVIVED_SUCCESS: set(),
    STATUS_FINAL_DELISTED: set(),
    STATUS_SKIPPED: set(),
    STATUS_FAILED: set(),
}

FAILURE_ALLOWED_FROM = STATUSES - TERMINAL_STATUSES - {STATUS_FAILED}

_ACTIVE_SQL = ",".join("?" for _ in ACTIVE_STATUSES)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cro_listing_lifecycle_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('revive_relist', 'final_delist')),
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    reason TEXT,
    old_listing_id TEXT,
    new_listing_id TEXT,
    old_offer_id TEXT,
    new_offer_id TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    metrics_before_json TEXT,
    db_snapshot_json TEXT,
    live_snapshot_json TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    started_at TEXT,
    finished_at TEXT,
    observe_until TEXT,
    observe_window_days INTEGER,
    conversion_help_attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    source TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cro_lifecycle_action_status
ON cro_listing_lifecycle_actions(action_type, status);

CREATE INDEX IF NOT EXISTS idx_cro_lifecycle_sku
ON cro_listing_lifecycle_actions(sku);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cro_lifecycle_active_sku
ON cro_listing_lifecycle_actions(sku)
WHERE status IN (
    'candidate',
    'approved',
    'prechecked',
    'old_withdrawn',
    'ready_to_publish',
    'published_new',
    'observing',
    'needs_conversion_help',
    'fallback_delist_pending',
    'failed'
);

CREATE TABLE IF NOT EXISTS cro_listing_lifecycle_action_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER,
    sku TEXT,
    from_status TEXT,
    to_status TEXT NOT NULL,
    operator TEXT,
    note TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cro_lifecycle_events_action
ON cro_listing_lifecycle_action_events(action_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    db = Path(db_path) if db_path else DEFAULT_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Phase 4 在 CREATE TABLE 里新增的列; 老库的既有表需要 ALTER 迁移,
# 否则 evaluate_observations 对每行报 "no such column" (2026-07-03 生产实况).
_LIFECYCLE_COLUMN_MIGRATIONS = {
    "observe_until":
        "ALTER TABLE cro_listing_lifecycle_actions ADD COLUMN observe_until TEXT",
    "observe_window_days":
        "ALTER TABLE cro_listing_lifecycle_actions ADD COLUMN observe_window_days INTEGER",
    "conversion_help_attempts":
        "ALTER TABLE cro_listing_lifecycle_actions "
        "ADD COLUMN conversion_help_attempts INTEGER NOT NULL DEFAULT 0",
}


def ensure_schema(db_path: Optional[Path] = None) -> None:
    """Create lifecycle tables and duplicate-protection indexes."""
    with _conn(db_path) as conn:
        conn.executescript(_SCHEMA)
        existing = _table_columns(conn, "cro_listing_lifecycle_actions")
        if existing:
            for column, ddl in _LIFECYCLE_COLUMN_MIGRATIONS.items():
                if column not in existing:
                    conn.execute(ddl)
        conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def _select_expr(columns: set[str], alias: str, column: str, default_sql: str) -> str:
    if column in columns:
        return f"{alias}.{column} AS {column}"
    return f"{default_sql} AS {column}"


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date()


def _parse_json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return fallback
    return fallback


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _active_action(conn: sqlite3.Connection, sku: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        f"""
        SELECT id, status, action_type
        FROM cro_listing_lifecycle_actions
        WHERE sku = ? AND status IN ({_ACTIVE_SQL})
        ORDER BY id DESC
        LIMIT 1
        """,
        (sku, *ACTIVE_STATUSES),
    ).fetchone()
    return dict(row) if row else None


def _relist_attempt_count(conn: sqlite3.Connection, sku: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM cro_listing_lifecycle_actions
        WHERE sku = ?
          AND action_type = ?
          AND status != ?
        """,
        (sku, ACTION_REVIVE_RELIST, STATUS_SKIPPED),
    ).fetchone()
    return int(row[0] if row else 0)


def _has_recent_relist(
    conn: sqlite3.Connection,
    sku: str,
    snapshot_day: date,
    window_days: int = 45,
) -> bool:
    cutoff = snapshot_day - timedelta(days=window_days)
    rows = conn.execute(
        """
        SELECT created_at, approved_at, started_at, finished_at
        FROM cro_listing_lifecycle_actions
        WHERE sku = ?
          AND action_type = ?
          AND status != ?
        """,
        (sku, ACTION_REVIVE_RELIST, STATUS_SKIPPED),
    ).fetchall()
    for row in rows:
        for field in ("finished_at", "started_at", "approved_at", "created_at"):
            row_day = _parse_date(row[field])
            if row_day is not None and row_day >= cutoff:
                return True
    return False


def _load_cro_blacklist(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "cro_diagnose_blacklist"):
        return set()
    try:
        return {
            str(row["sku"])
            for row in conn.execute("SELECT sku FROM cro_diagnose_blacklist")
            if row["sku"]
        }
    except sqlite3.OperationalError:
        return set()


def _load_operator_protected_skus(conn: sqlite3.Connection) -> set[str]:
    protected: set[str] = set()
    for table in ("cro_lifecycle_protected_skus", "operator_protected_skus"):
        if not _table_exists(conn, table):
            continue
        try:
            protected.update(
                str(row["sku"])
                for row in conn.execute(f"SELECT sku FROM {table}")
                if row["sku"]
            )
        except sqlite3.OperationalError:
            continue
    return protected


def _load_mi_blacklist(path: Optional[Path] = None) -> set[str]:
    blacklist_path = path or PROJECT_ROOT / "reports" / "mi_blacklist.json"
    if not blacklist_path.exists():
        return set()
    try:
        data = json.loads(blacklist_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, list):
        return {str(item) for item in data if item}
    if not isinstance(data, dict):
        return set()

    now = datetime.now(timezone.utc)
    active: set[str] = set()
    for sku, meta in data.items():
        if not sku:
            continue
        if not isinstance(meta, dict):
            active.add(str(sku))
            continue
        expires_raw = meta.get("expires_at") or meta.get("expires")
        if not expires_raw:
            active.add(str(sku))
            continue
        expires_text = str(expires_raw).strip()
        if expires_text.endswith("Z"):
            expires_text = f"{expires_text[:-1]}+00:00"
        try:
            expires = datetime.fromisoformat(expires_text)
        except ValueError:
            active.add(str(sku))
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires >= now:
            active.add(str(sku))
    return active


def _load_candidate_rows(conn: sqlite3.Connection, snapshot_date: str) -> list[dict[str, Any]]:
    snap_cols = _table_columns(conn, "cro_snapshots")
    metric_exprs = [
        "s.snapshot_date AS metrics_snapshot_date",
        "s.listing_id AS snapshot_listing_id" if "listing_id" in snap_cols else "NULL AS snapshot_listing_id",
        _select_expr(snap_cols, "s", "impressions", "0"),
        _select_expr(snap_cols, "s", "views", "0"),
        _select_expr(snap_cols, "s", "transactions", "0"),
        _select_expr(snap_cols, "s", "sold_qty", "0"),
    ]
    sql = f"""
        WITH latest_snapshot AS (
            SELECT sku, MAX(snapshot_date) AS snapshot_date
            FROM cro_snapshots
            WHERE snapshot_date <= ?
            GROUP BY sku
        )
        SELECT p.*, {", ".join(metric_exprs)}
        FROM collected_products p
        LEFT JOIN latest_snapshot latest ON latest.sku = p.sku
        LEFT JOIN cro_snapshots s
          ON s.sku = latest.sku
         AND s.snapshot_date = latest.snapshot_date
        WHERE UPPER(COALESCE(p.status, '')) = 'PUBLISHED'
        ORDER BY COALESCE(p.published_at, p.created_at), p.sku
    """
    return [dict(row) for row in conn.execute(sql, (snapshot_date,))]


def _latest_snapshot_date(conn: sqlite3.Connection) -> Optional[str]:
    if not _table_exists(conn, "cro_snapshots"):
        return None
    row = conn.execute("SELECT MAX(snapshot_date) FROM cro_snapshots").fetchone()
    return str(row[0]) if row and row[0] else None


def _record_event(
    conn: sqlite3.Connection,
    *,
    action_id: Optional[int],
    sku: Optional[str],
    from_status: Optional[str],
    to_status: str,
    operator: str = "system",
    note: str = "",
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO cro_listing_lifecycle_action_events
        (action_id, sku, from_status, to_status, operator, note, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (action_id, sku, from_status, to_status, operator, note, error, _now_iso()),
    )


def _build_candidate_reason(age_days: int, metrics: dict[str, Any]) -> str:
    return (
        f"age_days={age_days}; no_sales; "
        f"impressions={metrics['impressions']}; views={metrics['views']}; "
        f"age_source={metrics['age_source']}"
    )


def _is_old_dead_link(age_days: int, impressions: int, views: int) -> bool:
    return age_days >= 60 and impressions == 0 and views == 0


def detect_candidates(
    snapshot_date: str | None = None,
    min_age_days: int = 30,
    limit: int = 200,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Detect stale no-sale listings and persist manual-approval candidates."""
    ensure_schema(db_path)
    limit = max(0, int(limit))
    skipped: Counter[str] = Counter()
    created_rows: list[dict[str, Any]] = []

    with _conn(db_path) as conn:
        if not _table_exists(conn, "collected_products"):
            return {
                "snapshot_date": snapshot_date,
                "limit": limit,
                "scanned": 0,
                "created": 0,
                "candidates": [],
                "skipped": {"missing_collected_products": 1},
            }
        if not _table_exists(conn, "cro_snapshots"):
            return {
                "snapshot_date": snapshot_date,
                "limit": limit,
                "scanned": 0,
                "created": 0,
                "candidates": [],
                "skipped": {"missing_cro_snapshots": 1},
            }

        effective_snapshot = snapshot_date or _latest_snapshot_date(conn)
        if not effective_snapshot:
            return {
                "snapshot_date": effective_snapshot,
                "limit": limit,
                "scanned": 0,
                "created": 0,
                "candidates": [],
                "skipped": {"no_snapshot": 1},
            }
        snapshot_day = _parse_date(effective_snapshot)
        if snapshot_day is None:
            raise ValueError(f"Invalid snapshot_date: {effective_snapshot!r}")

        cro_blacklist = _load_cro_blacklist(conn)
        mi_blacklist = _load_mi_blacklist()
        protected_skus = _load_operator_protected_skus(conn)
        rows = _load_candidate_rows(conn, effective_snapshot)

        for product in rows:
            if len(created_rows) >= limit:
                skipped["limit_reached"] += 1
                break

            sku = str(product.get("sku") or "").strip()
            if not sku:
                skipped["missing_sku"] += 1
                continue
            if sku in cro_blacklist:
                skipped["cro_blacklisted"] += 1
                continue
            if sku in mi_blacklist:
                skipped["mi_blacklisted"] += 1
                continue
            if sku in protected_skus:
                skipped["operator_protected"] += 1
                continue
            if _active_action(conn, sku):
                skipped["duplicate_active_action"] += 1
                continue

            old_listing_id = str(
                product.get("listing_id") or product.get("snapshot_listing_id") or ""
            ).strip()
            if not old_listing_id:
                skipped["missing_listing_id"] += 1
                continue

            age_source = "published_at"
            age_start = _parse_date(product.get("published_at"))
            if age_start is None:
                age_start = _parse_date(product.get("created_at"))
                age_source = "created_at_fallback"
            if age_start is None:
                skipped["missing_age"] += 1
                continue
            age_days = (snapshot_day - age_start).days
            if age_days < int(min_age_days):
                skipped["too_young"] += 1
                continue

            if not product.get("metrics_snapshot_date"):
                skipped["no_snapshot"] += 1
                continue
            impressions = _int_value(product.get("impressions"))
            views = _int_value(product.get("views"))
            transactions = _int_value(product.get("transactions"))
            sold_qty = _int_value(product.get("sold_qty"))
            sales_detected = transactions > 0 or sold_qty > 0
            if sales_detected:
                skipped["sales_present"] += 1
                continue

            metrics = {
                "snapshot_date": product.get("metrics_snapshot_date"),
                "age_days": age_days,
                "age_source": age_source,
                "impressions": impressions,
                "views": views,
                "transactions": transactions,
                "sold_qty": sold_qty,
                "sales_detected": False,
            }
            reason = _build_candidate_reason(age_days, metrics)
            action_type = ACTION_REVIVE_RELIST
            priority = "P2" if impressions > 0 or views > 0 else "P3"
            attempts = _relist_attempt_count(conn, sku)

            if _is_old_dead_link(age_days, impressions, views):
                action_type = ACTION_FINAL_DELIST
                priority = "P3"
                reason = f"old_dead_link; {reason}; direct_final_delist"
            else:
                if attempts >= 1:
                    skipped["relist_attempt_limit"] += 1
                    continue
                if _has_recent_relist(conn, sku, snapshot_day):
                    skipped["recent_relist"] += 1
                    continue

                images = _parse_json_value(product.get("images"), [])
                if not isinstance(images, list) or len([img for img in images if img]) < 2:
                    skipped["insufficient_images"] += 1
                    continue
                if _int_value(product.get("stock")) <= 0:
                    skipped["out_of_stock"] += 1
                    continue

            now = _now_iso()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO cro_listing_lifecycle_actions
                    (sku, action_type, status, priority, reason, old_listing_id,
                     old_offer_id, attempt_no, metrics_before_json, db_snapshot_json,
                     created_at, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sku,
                        action_type,
                        STATUS_CANDIDATE,
                        priority,
                        reason,
                        old_listing_id,
                        product.get("offer_id") or product.get("ebay_offer_id"),
                        attempts + 1 if action_type == ACTION_REVIVE_RELIST else 1,
                        _json_dumps(metrics),
                        _json_dumps(product),
                        now,
                        f"detect_candidates:{effective_snapshot}",
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                skipped["duplicate_active_action"] += 1
                continue

            action_id = int(cur.lastrowid)
            _record_event(
                conn,
                action_id=action_id,
                sku=sku,
                from_status=None,
                to_status=STATUS_CANDIDATE,
                note="detected",
            )
            created_rows.append(
                {
                    "id": action_id,
                    "sku": sku,
                    "action_type": action_type,
                    "status": STATUS_CANDIDATE,
                    "priority": priority,
                    "reason": reason,
                    "old_listing_id": old_listing_id,
                    "attempt_no": attempts + 1 if action_type == ACTION_REVIVE_RELIST else 1,
                    "metrics": metrics,
                }
            )

        return {
            "snapshot_date": effective_snapshot,
            "limit": limit,
            "scanned": len(rows),
            "created": len(created_rows),
            "candidates": created_rows,
            "skipped": dict(skipped),
        }


def _get_action(conn: sqlite3.Connection, action_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM cro_listing_lifecycle_actions WHERE id = ?",
        (int(action_id),),
    ).fetchone()
    return dict(row) if row else None


def transition_action(
    action_id: int,
    new_status: str,
    *,
    operator: str = "system",
    note: str = "",
    error: Optional[str] = None,
    db_path: Path | None = None,
    **updates: Any,
) -> dict[str, Any]:
    """Move a lifecycle action through the documented state machine."""
    ensure_schema(db_path)
    if new_status not in STATUSES:
        raise ValueError(f"Unknown lifecycle status: {new_status}")

    conn = _conn(db_path)
    try:
        row = _get_action(conn, int(action_id))
        if row is None:
            raise KeyError(f"Lifecycle action not found: {action_id}")

        old_status = str(row["status"])
        allowed = new_status in ALLOWED_TRANSITIONS.get(old_status, set())
        if new_status == STATUS_FAILED and old_status in FAILURE_ALLOWED_FROM:
            allowed = True
        if not allowed:
            message = f"Invalid lifecycle transition: {old_status} -> {new_status}"
            _record_event(
                conn,
                action_id=int(action_id),
                sku=row.get("sku"),
                from_status=old_status,
                to_status=new_status,
                operator=operator,
                note=note,
                error=message,
            )
            conn.commit()
            raise ValueError(message)

        now = _now_iso()
        assignments: dict[str, Any] = {"status": new_status, "updated_at": now}
        if new_status == STATUS_APPROVED:
            assignments["approved_at"] = row.get("approved_at") or now
            assignments["approved_by"] = operator
        if new_status == STATUS_PRECHECKED:
            assignments["started_at"] = row.get("started_at") or now
        if new_status in TERMINAL_STATUSES:
            assignments["finished_at"] = row.get("finished_at") or now
        if error is not None:
            assignments["error"] = error
        for key, value in updates.items():
            if key in {
                "priority",
                "reason",
                "old_listing_id",
                "new_listing_id",
                "old_offer_id",
                "new_offer_id",
                "attempt_no",
                "metrics_before_json",
                "db_snapshot_json",
                "live_snapshot_json",
                "started_at",
                "finished_at",
                "observe_until",
                "observe_window_days",
                "conversion_help_attempts",
                "error",
                "source",
            }:
                assignments[key] = value

        set_sql = ", ".join(f"{key} = ?" for key in assignments)
        conn.execute(
            f"UPDATE cro_listing_lifecycle_actions SET {set_sql} WHERE id = ?",
            (*assignments.values(), int(action_id)),
        )
        _record_event(
            conn,
            action_id=int(action_id),
            sku=row.get("sku"),
            from_status=old_status,
            to_status=new_status,
            operator=operator,
            note=note,
            error=error,
        )
        conn.commit()
        updated = _get_action(conn, int(action_id))
        if updated is None:
            raise KeyError(f"Lifecycle action not found after update: {action_id}")
        return updated
    finally:
        conn.close()


def approve_actions(
    action_ids: list[int],
    operator: str = "system",
    db_path: Path | None = None,
) -> dict[str, int]:
    """Approve detected candidates for a later manual/rate-limited executor."""
    ensure_schema(db_path)
    result = {"approved": 0, "skipped": 0, "missing": 0}
    for action_id in action_ids:
        with _conn(db_path) as conn:
            row = _get_action(conn, int(action_id))
        if row is None:
            result["missing"] += 1
            continue
        if row["status"] != STATUS_CANDIDATE:
            result["skipped"] += 1
            continue
        transition_action(
            int(action_id),
            STATUS_APPROVED,
            operator=operator,
            note="approved",
            db_path=db_path,
        )
        result["approved"] += 1
    return result


def _latest_snapshot_for_sku(conn: sqlite3.Connection, sku: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "cro_snapshots"):
        return None
    row = conn.execute(
        """
        SELECT *
        FROM cro_snapshots
        WHERE sku = ?
        ORDER BY snapshot_date DESC
        LIMIT 1
        """,
        (sku,),
    ).fetchone()
    return dict(row) if row else None


def _product_for_sku(conn: sqlite3.Connection, sku: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "collected_products"):
        return None
    row = conn.execute(
        "SELECT * FROM collected_products WHERE sku = ? LIMIT 1",
        (sku,),
    ).fetchone()
    return dict(row) if row else None


def _product_for_publish(product: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(product)
    list_fields = ("images", "videos")
    dict_fields = ("attributes", "specs", "cost_breakdown", "optimization")

    for field in list_fields:
        value = decoded.get(field)
        if isinstance(value, str):
            parsed = _parse_json_value(value, [])
            decoded[field] = parsed if isinstance(parsed, list) else []
        elif value is None:
            decoded[field] = []

    for field in dict_fields:
        value = decoded.get(field)
        if isinstance(value, str):
            parsed = _parse_json_value(value, {})
            decoded[field] = parsed if isinstance(parsed, dict) else {}
        elif value is None:
            decoded[field] = {}

    return decoded


def _update_product_for_republish(conn: sqlite3.Connection, sku: str) -> None:
    columns = _table_columns(conn, "collected_products")
    assignments: dict[str, Any] = {"status": "READY_TO_PUBLISH"}
    if "listing_id" in columns:
        assignments["listing_id"] = None
    if "published_at" in columns:
        assignments["published_at"] = None
    if "updated_at" in columns:
        assignments["updated_at"] = _now_iso()
    set_sql = ", ".join(f"{key} = ?" for key in assignments)
    conn.execute(
        f"UPDATE collected_products SET {set_sql} WHERE sku = ?",
        (*assignments.values(), sku),
    )


def _mark_product_published(
    conn: sqlite3.Connection,
    sku: str,
    listing_id: str,
) -> None:
    columns = _table_columns(conn, "collected_products")
    assignments: dict[str, Any] = {"status": "PUBLISHED"}
    if "listing_id" in columns:
        assignments["listing_id"] = listing_id
    if "published_at" in columns:
        assignments["published_at"] = _now_iso()
    if "updated_at" in columns:
        assignments["updated_at"] = _now_iso()
    set_sql = ", ".join(f"{key} = ?" for key in assignments)
    conn.execute(
        f"UPDATE collected_products SET {set_sql} WHERE sku = ?",
        (*assignments.values(), sku),
    )


def _parse_trading_ack(xml_text: str) -> tuple[str, list[dict[str, str]]]:
    ns = {"ebay": "urn:ebay:apis:eBLBaseComponents"}
    root = ET.fromstring(xml_text)
    ack = root.findtext("ebay:Ack", default="", namespaces=ns)
    errors: list[dict[str, str]] = []
    for err in root.findall(".//ebay:Errors", ns):
        errors.append({
            "code": err.findtext("ebay:ErrorCode", default="", namespaces=ns),
            "severity": err.findtext("ebay:SeverityCode", default="", namespaces=ns),
            "message": (
                err.findtext("ebay:LongMessage", default="", namespaces=ns)
                or err.findtext("ebay:ShortMessage", default="", namespaces=ns)
            ),
        })
    return ack, errors


def _default_trading_client():
    from src.services.listing_status_sync import _get_trading_client
    return _get_trading_client()


def _default_dry_run_publish(product: dict[str, Any]) -> dict[str, Any]:
    from batch_publish import publish_single_product
    return publish_single_product(product, dry_run=True)


def _default_publish(product: dict[str, Any]) -> dict[str, Any]:
    from batch_publish import publish_single_product
    return publish_single_product(product, dry_run=False)


def _precheck_errors(action: dict[str, Any],
                     product: dict[str, Any] | None,
                     snapshot: dict[str, Any] | None) -> list[str]:
    errors: list[str] = []
    if not product:
        return ["product_missing"]

    product_status = str(product.get("status") or "").upper()
    if product_status != "PUBLISHED":
        errors.append(f"not_published:{product_status or 'blank'}")

    product_listing_id = str(product.get("listing_id") or "").strip()
    old_listing_id = str(action.get("old_listing_id") or "").strip()
    if not product_listing_id:
        errors.append("listing_id_missing")
    elif old_listing_id and product_listing_id != old_listing_id:
        errors.append("listing_id_mismatch")

    images = _parse_json_value(product.get("images"), [])
    image_count = len([img for img in images if img]) if isinstance(images, list) else 0
    if image_count < 2:
        errors.append("insufficient_images")

    if _int_value(product.get("stock")) <= 0:
        errors.append("out_of_stock")

    price = product.get("suggested_price") or product.get("price")
    try:
        if float(price or 0) <= 0:
            errors.append("price_missing")
    except (TypeError, ValueError):
        errors.append("price_missing")

    if snapshot:
        if _int_value(snapshot.get("transactions")) > 0 or _int_value(snapshot.get("sold_qty")) > 0:
            errors.append("sales_present")
    return errors


def precheck_approved(
    limit: int = 50,
    apply_changes: bool = False,
    operator: str = "system",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Precheck approved revive actions before any live withdraw/re-publish step."""
    ensure_schema(db_path)
    with _conn(db_path) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cro_listing_lifecycle_actions
                WHERE action_type = ? AND status = ?
                ORDER BY approved_at, id
                LIMIT ?
                """,
                (ACTION_REVIVE_RELIST, STATUS_APPROVED, max(0, int(limit))),
            )
        ]

    results: list[dict[str, Any]] = []
    for action in rows:
        action_id = int(action["id"])
        sku = str(action["sku"])
        with _conn(db_path) as conn:
            product = _product_for_sku(conn, sku)
            snapshot = _latest_snapshot_for_sku(conn, sku)
        errors = _precheck_errors(action, product, snapshot)
        db_snapshot = {
            "product": product or {},
            "latest_snapshot": snapshot or {},
            "checked_at": _now_iso(),
        }
        live_snapshot = {
            "source": "db_precheck",
            "old_listing_id": action.get("old_listing_id"),
            "product_listing_id": (product or {}).get("listing_id"),
        }
        if errors:
            status = STATUS_SKIPPED
            error = ";".join(errors)
            if apply_changes:
                transition_action(
                    action_id,
                    STATUS_SKIPPED,
                    operator=operator,
                    note="precheck_failed",
                    error=error,
                    db_snapshot_json=_json_dumps(db_snapshot),
                    live_snapshot_json=_json_dumps(live_snapshot),
                    db_path=db_path,
                )
            results.append({
                "id": action_id,
                "sku": sku,
                "status": status,
                "errors": errors,
            })
            continue

        if apply_changes:
            transition_action(
                action_id,
                STATUS_PRECHECKED,
                operator=operator,
                note="prechecked",
                db_snapshot_json=_json_dumps(db_snapshot),
                live_snapshot_json=_json_dumps(live_snapshot),
                db_path=db_path,
            )
        results.append({
            "id": action_id,
            "sku": sku,
            "status": STATUS_PRECHECKED,
            "errors": [],
        })

    return {
        "apply_changes": bool(apply_changes),
        "selected": len(rows),
        "prechecked": sum(1 for row in results if row["status"] == STATUS_PRECHECKED),
        "skipped": sum(1 for row in results if row["status"] == STATUS_SKIPPED),
        "results": results,
    }


def execute_prechecked_relist(
    limit: int = 10,
    apply_changes: bool = False,
    operator: str = "system",
    db_path: Path | None = None,
    trading_client: Any = None,
    dry_run_publish_func: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    publish_func: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Withdraw old live listings and republish prechecked revive actions.

    The publish dry-run is executed before withdrawing the old listing so local
    blockers do not strand a live SKU.
    """
    ensure_schema(db_path)
    with _conn(db_path) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cro_listing_lifecycle_actions
                WHERE action_type = ? AND status = ?
                ORDER BY id
                LIMIT ?
                """,
                (ACTION_REVIVE_RELIST, STATUS_PRECHECKED, max(0, int(limit))),
            )
        ]

    if not apply_changes:
        return {
            "apply_changes": False,
            "selected": len(rows),
            "actions": rows,
        }

    trading = trading_client or _default_trading_client()
    dry_run = dry_run_publish_func or _default_dry_run_publish
    publish = publish_func or _default_publish
    results: list[dict[str, Any]] = []

    for action in rows:
        action_id = int(action["id"])
        sku = str(action["sku"])
        old_listing_id = str(action.get("old_listing_id") or "").strip()
        old_withdrawn = False
        try:
            with _conn(db_path) as conn:
                product = _product_for_sku(conn, sku)
                snapshot = _latest_snapshot_for_sku(conn, sku)
            recheck_errors = _precheck_errors(action, product, snapshot)
            if recheck_errors:
                error = ";".join(recheck_errors)
                transition_action(
                    action_id,
                    STATUS_SKIPPED,
                    operator=operator,
                    note="phase3_recheck_failed",
                    error=error,
                    db_snapshot_json=_json_dumps({
                        "product": product or {},
                        "latest_snapshot": snapshot or {},
                        "checked_at": _now_iso(),
                    }),
                    db_path=db_path,
                )
                results.append({
                    "id": action_id,
                    "sku": sku,
                    "status": STATUS_SKIPPED,
                    "error": error,
                })
                continue
            if not product:
                raise ValueError("product_missing")
            if str(product.get("status") or "").upper() != "PUBLISHED":
                raise ValueError(f"product_not_published:{product.get('status')}")
            if not old_listing_id:
                raise ValueError("old_listing_id_missing")
            if str(product.get("listing_id") or "").strip() != old_listing_id:
                raise ValueError("old_listing_id_mismatch")

            dry_run_result = dry_run(_product_for_publish(product))
            if dry_run_result.get("status") not in {"dry_run", "success"}:
                error = f"dry_run_failed:{dry_run_result.get('message', '')}"
                transition_action(
                    action_id,
                    STATUS_SKIPPED,
                    operator=operator,
                    note="phase3_dry_run_failed",
                    error=error,
                    db_path=db_path,
                )
                results.append({
                    "id": action_id,
                    "sku": sku,
                    "status": STATUS_SKIPPED,
                    "error": error,
                    "dry_run": dry_run_result,
                })
                continue

            withdraw_xml = trading.end_item(old_listing_id, reason="NotAvailable")
            ack, errors = _parse_trading_ack(withdraw_xml)
            if ack not in {"Success", "Warning"}:
                raise RuntimeError(f"withdraw_failed:{ack}:{errors}")
            old_withdrawn = True
            transition_action(
                action_id,
                STATUS_OLD_WITHDRAWN,
                operator=operator,
                note="phase3_old_listing_withdrawn",
                live_snapshot_json=_json_dumps({
                    "old_listing_id": old_listing_id,
                    "withdraw_ack": ack,
                    "withdraw_errors": errors,
                    "withdrawn_at": _now_iso(),
                }),
                db_path=db_path,
            )

            with _conn(db_path) as conn:
                _update_product_for_republish(conn, sku)
                conn.commit()
            transition_action(
                action_id,
                STATUS_READY_TO_PUBLISH,
                operator=operator,
                note="phase3_ready_to_publish",
                db_path=db_path,
            )

            with _conn(db_path) as conn:
                ready_product = _product_for_sku(conn, sku)
            if not ready_product:
                raise ValueError("product_missing_after_ready")
            publish_result = publish(_product_for_publish(ready_product))
            if publish_result.get("status") != "success" or not publish_result.get("listing_id"):
                raise RuntimeError(f"publish_failed:{publish_result.get('message', '')}")
            new_listing_id = str(publish_result.get("listing_id") or "")
            with _conn(db_path) as conn:
                _mark_product_published(conn, sku, new_listing_id)
                conn.commit()
            transition_action(
                action_id,
                STATUS_PUBLISHED_NEW,
                operator=operator,
                note="phase3_published_new",
                new_listing_id=new_listing_id,
                new_offer_id=str(publish_result.get("offer_id") or ""),
                db_path=db_path,
            )
            results.append({
                "id": action_id,
                "sku": sku,
                "status": STATUS_PUBLISHED_NEW,
                "old_listing_id": old_listing_id,
                "old_withdrawn": old_withdrawn,
                "new_listing_id": publish_result.get("listing_id"),
                "new_offer_id": publish_result.get("offer_id"),
                "publish": publish_result,
            })
        except Exception as exc:
            error = str(exc)
            try:
                transition_action(
                    action_id,
                    STATUS_FAILED,
                    operator=operator,
                    note="phase3_failed",
                    error=error,
                    db_path=db_path,
                )
            except Exception:
                pass
            results.append({
                "id": action_id,
                "sku": sku,
                "status": STATUS_FAILED,
                "old_withdrawn": old_withdrawn,
                "error": error,
            })

    return {
        "apply_changes": True,
        "selected": len(rows),
        "old_withdrawn": sum(1 for row in results if row.get("old_withdrawn")),
        "published_new": sum(1 for row in results if row["status"] == STATUS_PUBLISHED_NEW),
        "skipped": sum(1 for row in results if row["status"] == STATUS_SKIPPED),
        "failed": sum(1 for row in results if row["status"] == STATUS_FAILED),
        "results": results,
    }


def execute_approved(
    limit: int = 10,
    apply_changes: bool = False,
    operator: str = "system",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Thin orchestrator over the safe two-step flow.

    等价于依次执行 `precheck_approved` → `execute_prechecked_relist`,
    两步共用同一 apply_changes / limit. 不存在绕过 precheck 的直发路径:
    执行阶段只消费 precheck 通过 (status=prechecked) 的行, 销量复查、
    live 状态复查与 withdraw/republish 安全序全部由这两个阶段函数负责.

    dry-run (apply_changes=False) 时 precheck 不落状态转移, 因此 execute
    阶段只会看到此前已 prechecked 的行 — 报告如实反映两步各自的可见范围.
    """
    ensure_schema(db_path)
    precheck_report = precheck_approved(
        limit=limit,
        apply_changes=apply_changes,
        operator=operator,
        db_path=db_path,
    )
    execute_report = execute_prechecked_relist(
        limit=limit,
        apply_changes=apply_changes,
        operator=operator,
        db_path=db_path,
    )
    return {
        "apply_changes": bool(apply_changes),
        "selected": precheck_report.get("selected", 0),
        "precheck": precheck_report,
        "execute": execute_report,
    }


def evaluate_observations(
    snapshot_date: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Phase 4 observation evaluation.
    
    1. Transitions published_new to observing.
    2. Evaluates observing rows against thresholds and resolves to terminal/fallback states.
    """
    from src.services.cro_thresholds import get_lifecycle_thresholds
    from src.services.cro_action_queue import enqueue_unique_pending
    ensure_schema(db_path)
    thresholds = get_lifecycle_thresholds()
    
    with _conn(db_path) as conn:
        new_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM cro_listing_lifecycle_actions WHERE status = ?",
            (STATUS_PUBLISHED_NEW,)
        )]
        effective_snapshot = snapshot_date or _latest_snapshot_date(conn)
    
    now = datetime.now(timezone.utc)
    started = 0
    for row in new_rows:
        try:
            window = thresholds["observe_window_days_p3"] if row["priority"] == "P3" else thresholds["observe_window_days_p2"]
            updated_text = str(row.get("updated_at") or row.get("finished_at") or _now_iso()).replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(updated_text)
            except ValueError:
                dt = now
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            
            observe_until = (dt + timedelta(days=window)).isoformat()
            
            transition_action(
                row["id"],
                STATUS_OBSERVING,
                observe_window_days=window,
                observe_until=observe_until,
                db_path=db_path
            )
            started += 1
        except Exception as exc:
            logger.warning(f"Observation error action={row.get('id')} sku={row.get('sku')}: {exc}")

    with _conn(db_path) as conn:
        observing_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM cro_listing_lifecycle_actions WHERE status = ?",
            (STATUS_OBSERVING,)
        )]

    results = []
    floor_imp = thresholds["revive_traffic_floor_impressions"]
    floor_views = thresholds["revive_traffic_floor_views"]
    
    for row in observing_rows:
        action_id = row["id"]
        sku = row["sku"]
        with _conn(db_path) as conn:
            snapshot = _latest_snapshot_for_sku(conn, sku) or {}
            
        transactions = _int_value(snapshot.get("transactions"))
        sold_qty = _int_value(snapshot.get("sold_qty"))
        if transactions > 0 or sold_qty > 0:
            try:
                transition_action(
                    action_id,
                    STATUS_REVIVED_SUCCESS,
                    note="sale_detected",
                    db_path=db_path
                )
                results.append({"id": action_id, "sku": sku, "status": STATUS_REVIVED_SUCCESS, "reason": "sale"})
            except Exception as exc:
                logger.warning(f"Observation error action={action_id} sku={sku}: {exc}")
            continue
            
        observe_until_text = str(row.get("observe_until") or "").replace("Z", "+00:00")
        try:
            until_dt = datetime.fromisoformat(observe_until_text)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            until_dt = now
            
        if now < until_dt:
            continue
            
        before = _parse_json_value(row.get("metrics_before_json"), {})
        before_imp = _int_value(before.get("impressions"))
        before_views = _int_value(before.get("views"))
        
        snap_imp = _int_value(snapshot.get("impressions"))
        snap_views = _int_value(snapshot.get("views"))
        
        traffic_recovered = (
            snap_imp >= floor_imp and
            snap_views >= floor_views and
            snap_imp >= before_imp and
            snap_views >= before_views
        )
        
        try:
            if traffic_recovered:
                attempts = _int_value(row.get("conversion_help_attempts"))
                if attempts < 1:
                    enqueue_unique_pending([{
                        "sku": sku,
                        "action": "promoted_listings",
                        "priority": "P2",
                        "reason": "lifecycle_needs_conversion_help"
                    }, {
                        "sku": sku,
                        "action": "reprice",
                        "priority": "P2",
                        "reason": "lifecycle_needs_conversion_help"
                    }], source="cro_lifecycle_help")
                    
                    transition_action(
                        action_id,
                        STATUS_NEEDS_CONVERSION_HELP,
                        note="traffic_recovered_needs_help",
                        db_path=db_path
                    )
                    
                    window = _int_value(row.get("observe_window_days")) or thresholds["observe_window_days_p2"]
                    new_until = (now + timedelta(days=window)).isoformat()
                    
                    transition_action(
                        action_id,
                        STATUS_OBSERVING,
                        observe_until=new_until,
                        conversion_help_attempts=attempts + 1,
                        note="re_observe_after_help",
                        db_path=db_path
                    )
                    results.append({"id": action_id, "sku": sku, "status": STATUS_NEEDS_CONVERSION_HELP, "reason": "traffic_recovered"})
                else:
                    transition_action(
                        action_id,
                        STATUS_FALLBACK_DELIST_PENDING,
                        note="traffic_recovered_but_max_attempts_reached",
                        db_path=db_path
                    )
                    results.append({"id": action_id, "sku": sku, "status": STATUS_FALLBACK_DELIST_PENDING, "reason": "max_help_attempts"})
            else:
                transition_action(
                    action_id,
                    STATUS_FALLBACK_DELIST_PENDING,
                    note="traffic_dead_after_window",
                    db_path=db_path
                )
                results.append({"id": action_id, "sku": sku, "status": STATUS_FALLBACK_DELIST_PENDING, "reason": "traffic_dead"})
        except Exception as exc:
            logger.warning(f"Observation error action={action_id} sku={sku}: {exc}")

    return {
        "snapshot_date": effective_snapshot,
        "started_observing": started,
        "evaluated": len(results),
        "observing_remaining": len(observing_rows) - len(results) + started,
        "results": results,
    }
