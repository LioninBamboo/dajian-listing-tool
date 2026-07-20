"""Fail-closed runtime checks for the production SQLite database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


class DatabaseSafetyError(RuntimeError):
    """Raised when the runtime database is unsafe to open for production work."""


@dataclass(frozen=True)
class DatabaseSafetyReport:
    path: Path
    link_count: int
    integrity_check: str
    page_count: int
    size_bytes: int


def assert_runtime_not_in_maintenance(path: str | Path) -> None:
    """Fail closed when an operator maintenance lock is present."""

    maintenance_file = Path(path).resolve()
    if maintenance_file.exists():
        raise DatabaseSafetyError(f"maintenance lock is active: {maintenance_file}")


def assert_single_link_database(path: str | Path) -> tuple[Path, int]:
    """Return the resolved database path and reject SQLite hard links."""

    database = Path(path).resolve()
    if not database.is_file():
        raise DatabaseSafetyError(f"production database does not exist: {database}")
    link_count = int(database.stat().st_nlink)
    if link_count != 1:
        raise DatabaseSafetyError(
            f"production database has {link_count} hard links: {database}; "
            "each SQLite path would use a different WAL/SHM sidecar"
        )
    return database, link_count


def validate_runtime_database(path: str | Path) -> DatabaseSafetyReport:
    """Reject missing, hardlinked, or structurally malformed SQLite files."""

    database, link_count = assert_single_link_database(path)
    stat = database.stat()
    with database.open("rb") as handle:
        header = handle.read(16)
    if header != b"SQLite format 3\x00":
        raise DatabaseSafetyError(f"invalid SQLite header for {database}")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
        checks = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        has_products = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collected_products'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise DatabaseSafetyError(
            f"database integrity check failed for {database}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    if checks != ["ok"]:
        preview = "; ".join(checks[:5])
        raise DatabaseSafetyError(
            f"database integrity check failed for {database}: {preview}"
        )
    if page_count <= 0:
        raise DatabaseSafetyError(f"invalid database page count for {database}: {page_count}")
    if not has_products:
        raise DatabaseSafetyError(
            f"expected collected_products table is missing from {database}"
        )

    return DatabaseSafetyReport(
        path=database,
        link_count=link_count,
        integrity_check="ok",
        page_count=page_count,
        size_bytes=stat.st_size,
    )
