"""Safe, local SQLite migration for the initial QC experience registry."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.db.database_safety import (
    DatabaseSafetyReport,
    assert_runtime_not_in_maintenance,
    validate_runtime_database,
)
from src.services.qc_experience_seed import seed_initial_qc_experiences


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "ebay_collection.db"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups"
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}_[0-9]{6}$")
_REGISTRY_TABLES = ("qc_experiences", "qc_rule_catalog")


class QCExperienceMigrationError(RuntimeError):
    """Raised when the registry migration cannot be completed safely."""


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
    else:
        connection = sqlite3.connect(str(path), timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    )


def _registry_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name in _REGISTRY_TABLES:
        counts["experiences" if table_name == "qc_experiences" else "rules"] = (
            int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
            if _table_exists(connection, table_name)
            else 0
        )
    return counts


def _integrity_check(connection: sqlite3.Connection) -> str:
    checks = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if checks != ["ok"]:
        preview = "; ".join(checks[:5])
        raise QCExperienceMigrationError(f"SQLite integrity check failed: {preview}")
    return "ok"


def _timestamp(value: str | None) -> str:
    candidate = value or datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    if not _TIMESTAMP_RE.fullmatch(candidate):
        raise ValueError("timestamp must use YYYYMMDD_HHMMSS")
    return candidate


def _backup_path(backup_dir: Path, timestamp: str) -> Path:
    return backup_dir / f"ebay_collection_pre_qc_experience_seed_{timestamp}.db"


def _create_online_backup(source: sqlite3.Connection, destination: Path) -> DatabaseSafetyReport:
    """Create and validate a new backup without overwriting an existing file."""

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise QCExperienceMigrationError(f"backup already exists: {destination}")

    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        raise QCExperienceMigrationError(f"temporary backup already exists: {temporary}")

    try:
        target = sqlite3.connect(str(temporary), timeout=30)
        try:
            target.execute("PRAGMA busy_timeout=30000")
            source.backup(target, pages=256, sleep=0.05)
            target.commit()
        finally:
            target.close()
        validate_runtime_database(temporary)
        temporary.replace(destination)
        return validate_runtime_database(destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def run_qc_experience_seed_migration(
    db_path: str | Path = DEFAULT_DB_PATH,
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    *,
    apply: bool = False,
    timestamp: str | None = None,
    maintenance_lock: str | Path | None = None,
    seed_fn: Callable[..., dict[str, int]] = seed_initial_qc_experiences,
) -> dict[str, object]:
    """Run a dry-run or an atomic, backed-up registry seed migration.

    The default is read-only.  ``apply=True`` takes an IMMEDIATE SQLite
    transaction, snapshots the pre-migration state through SQLite's online
    backup API, seeds only the additive registry tables, commits, and then
    validates both source and backup integrity.
    """

    database = Path(db_path).resolve()
    backups = Path(backup_dir).resolve()
    if maintenance_lock is not None:
        assert_runtime_not_in_maintenance(maintenance_lock)

    preflight = validate_runtime_database(database)
    stamp = _timestamp(timestamp)
    connection = _connect(database, read_only=True)
    try:
        counts_before = _registry_counts(connection)
    finally:
        connection.close()

    if not apply:
        return {
            "status": "dry_run",
            "db_path": str(database),
            "backup_path": None,
            "counts_before": counts_before,
            "counts_after": dict(counts_before),
            "integrity_check": preflight.integrity_check,
            "backup_integrity_check": None,
            "seed": {"experiences": 6, "rules": 6},
        }

    destination = _backup_path(backups, stamp)
    backup_source = _connect(database, read_only=True)
    try:
        backup_report = _create_online_backup(backup_source, destination)
    finally:
        backup_source.close()

    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        counts_before = _registry_counts(connection)
        seed_fn(connection, commit=False)
        connection.commit()
        counts_after = _registry_counts(connection)
        integrity_check = _integrity_check(connection)
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, QCExperienceMigrationError):
            raise
        raise QCExperienceMigrationError(f"QC experience migration rolled back: {exc}") from exc
    finally:
        connection.close()

    postflight = validate_runtime_database(database)
    if backup_report is None:
        raise QCExperienceMigrationError("migration completed without a validated backup")
    return {
        "status": "applied",
        "db_path": str(database),
        "backup_path": str(destination),
        "counts_before": counts_before,
        "counts_after": counts_after,
        "integrity_check": postflight.integrity_check if postflight else integrity_check,
        "backup_integrity_check": backup_report.integrity_check,
        "seed": {"experiences": 6, "rules": 6},
    }
