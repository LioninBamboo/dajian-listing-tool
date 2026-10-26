"""Shared online SQLite backup used by operator write tools."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.db.database_safety import DatabaseSafetyReport, validate_runtime_database


class DbBackupError(RuntimeError):
    """Backup path collision or other backup-specific refusal."""


def create_online_backup(
    source: sqlite3.Connection,
    destination: Path,
) -> DatabaseSafetyReport:
    """Create and validate a new backup without overwriting an existing file.

    Behavior matches the previous ``qc_experience_migration._create_online_backup``:
    sqlite ``backup()`` to ``.tmp``, ``validate_runtime_database``, then atomic rename.
    """

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise DbBackupError(f"backup already exists: {destination}")

    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        raise DbBackupError(f"temporary backup already exists: {temporary}")

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
