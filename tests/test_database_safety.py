import os
import sqlite3

import pytest

import src.db.database_safety as database_safety
from src.db.database_safety import (
    DatabaseSafetyError,
    assert_runtime_not_in_maintenance,
    validate_runtime_database,
)


def _create_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE collected_products (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO collected_products(value) VALUES ('ok')")


def test_clean_single_link_database_passes(tmp_path):
    database = tmp_path / "clean.db"
    _create_database(database)

    report = validate_runtime_database(database)

    assert report.integrity_check == "ok"
    assert report.link_count == 1


def test_hardlinked_database_is_rejected(tmp_path):
    database = tmp_path / "production.db"
    hardlink = tmp_path / "worktree.db"
    _create_database(database)
    os.link(database, hardlink)

    with pytest.raises(DatabaseSafetyError, match="hard link"):
        validate_runtime_database(database)


def test_malformed_database_is_rejected(tmp_path):
    database = tmp_path / "malformed.db"
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(DatabaseSafetyError, match="SQLite header|integrity check failed"):
        validate_runtime_database(database)


def test_empty_database_is_rejected(tmp_path):
    database = tmp_path / "empty.db"
    database.touch()

    with pytest.raises(DatabaseSafetyError, match="SQLite header|page count"):
        validate_runtime_database(database)


def test_database_without_expected_schema_is_rejected(tmp_path):
    database = tmp_path / "wrong-schema.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    with pytest.raises(DatabaseSafetyError, match="collected_products"):
        validate_runtime_database(database)


def test_zero_page_database_is_rejected_even_if_other_checks_claim_success(tmp_path, monkeypatch):
    database = tmp_path / "zero-pages.db"
    database.write_bytes(b"SQLite format 3\x00")

    class FakeResult:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class FakeConnection:
        def execute(self, statement):
            if statement == "PRAGMA quick_check":
                return FakeResult([("ok",)])
            if statement == "PRAGMA page_count":
                return FakeResult([(0,)])
            return FakeResult([(1,)])

        def close(self):
            pass

    monkeypatch.setattr(database_safety.sqlite3, "connect", lambda *args, **kwargs: FakeConnection())

    with pytest.raises(DatabaseSafetyError, match="page count"):
        validate_runtime_database(database)


def test_runtime_is_rejected_while_maintenance_lock_exists(tmp_path):
    maintenance = tmp_path / "_maintenance.lock"
    maintenance.write_text("recovery in progress", encoding="utf-8")

    with pytest.raises(DatabaseSafetyError, match="maintenance lock"):
        assert_runtime_not_in_maintenance(maintenance)


def test_runtime_passes_without_maintenance_lock(tmp_path):
    assert_runtime_not_in_maintenance(tmp_path / "_maintenance.lock")
