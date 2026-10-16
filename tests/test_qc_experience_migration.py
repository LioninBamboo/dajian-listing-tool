import sqlite3

import pytest

from src.services.qc_experience_migration import QCExperienceMigrationError
from src.services.qc_experience_migration import run_qc_experience_seed_migration
from src.services.qc_experience_registry import ensure_qc_experience_registry, register_experience


def _create_runtime_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE collected_products (id INTEGER PRIMARY KEY, sku TEXT)"
        )
        conn.execute("INSERT INTO collected_products (sku) VALUES ('SKU-1')")


def test_dry_run_is_read_only_and_reports_empty_registry(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    backup_dir = tmp_path / "backups"
    _create_runtime_db(db_path)

    result = run_qc_experience_seed_migration(
        db_path,
        backup_dir,
        apply=False,
        timestamp="20260804_190000",
    )

    assert result["status"] == "dry_run"
    assert result["counts_before"] == {"experiences": 0, "rules": 0}
    assert result["counts_after"] == result["counts_before"]
    assert result["backup_path"] is None
    assert not backup_dir.exists()


def test_apply_creates_verified_backup_and_seeds_registry(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    backup_dir = tmp_path / "backups"
    _create_runtime_db(db_path)

    result = run_qc_experience_seed_migration(
        db_path,
        backup_dir,
        apply=True,
        timestamp="20260804_190001",
    )

    backup_path = backup_dir / "ebay_collection_pre_qc_experience_seed_20260804_190001.db"
    assert result["status"] == "applied"
    assert result["counts_before"] == {"experiences": 0, "rules": 0}
    assert result["counts_after"] == {"experiences": 6, "rules": 6}
    assert result["integrity_check"] == "ok"
    assert result["backup_integrity_check"] == "ok"
    assert backup_path.is_file()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM qc_experiences").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM qc_rule_catalog").fetchone()[0] == 6

    with sqlite3.connect(backup_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qc_experiences'"
        ).fetchone() is None


def test_apply_is_idempotent_for_registry_rows(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    backup_dir = tmp_path / "backups"
    _create_runtime_db(db_path)

    first = run_qc_experience_seed_migration(
        db_path,
        backup_dir,
        apply=True,
        timestamp="20260804_190002",
    )
    second = run_qc_experience_seed_migration(
        db_path,
        backup_dir,
        apply=True,
        timestamp="20260804_190003",
    )

    assert first["counts_after"] == {"experiences": 6, "rules": 6}
    assert second["counts_before"] == {"experiences": 6, "rules": 6}
    assert second["counts_after"] == second["counts_before"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM qc_experiences").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM qc_rule_catalog").fetchone()[0] == 6


def test_failed_seed_rolls_back_schema_changes_and_keeps_pre_seed_backup(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    backup_dir = tmp_path / "backups"
    _create_runtime_db(db_path)

    def failing_seed(conn, *, commit=False):
        conn.execute("CREATE TABLE qc_partial_write (id INTEGER PRIMARY KEY)")
        raise RuntimeError("synthetic seed failure")

    with pytest.raises(QCExperienceMigrationError, match="rolled back"):
        run_qc_experience_seed_migration(
            db_path,
            backup_dir,
            apply=True,
            timestamp="20260804_190004",
            seed_fn=failing_seed,
        )

    backup_path = backup_dir / "ebay_collection_pre_qc_experience_seed_20260804_190004.db"
    assert backup_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qc_partial_write'"
        ).fetchone() is None


def test_failed_registry_seed_rolls_back_registry_tables_and_rows(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    backup_dir = tmp_path / "backups"
    _create_runtime_db(db_path)

    def failing_seed(conn, *, commit=False):
        ensure_qc_experience_registry(conn, commit=False)
        register_experience(
            conn,
            experience_id="EXP-ROLLBACK",
            title="Rollback test",
            domain="test",
            root_cause="synthetic_failure",
            evidence={"test": True},
            fix_action="rollback",
            commit=False,
        )
        raise RuntimeError("synthetic registry failure")

    with pytest.raises(QCExperienceMigrationError, match="rolled back"):
        run_qc_experience_seed_migration(
            db_path,
            backup_dir,
            apply=True,
            timestamp="20260804_190007",
            seed_fn=failing_seed,
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qc_experiences'"
        ).fetchone() is None
