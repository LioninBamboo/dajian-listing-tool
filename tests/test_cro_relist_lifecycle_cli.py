"""CLI coverage for CRO relist lifecycle dry-run/apply flows."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import src.services.cro_relist_lifecycle as lifecycle


@pytest.fixture(autouse=True)
def _isolate_mi_blacklist(monkeypatch):
    monkeypatch.setattr(lifecycle, "_load_mi_blacklist", lambda path=None: set())


def _create_source_tables(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE collected_products (
                sku TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                listing_id TEXT,
                images TEXT,
                stock INTEGER,
                published_at TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE cro_snapshots (
                snapshot_date TEXT NOT NULL,
                sku TEXT NOT NULL,
                listing_id TEXT,
                impressions INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0,
                transactions INTEGER DEFAULT 0,
                sold_qty INTEGER DEFAULT 0,
                PRIMARY KEY (snapshot_date, sku)
            )
            """
        )
        conn.commit()


def _seed_product(
    db_path: Path,
    sku: str,
    *,
    published_at: str,
    impressions: int,
    views: int,
) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO collected_products
            (sku, title, status, listing_id, images, stock, published_at, created_at)
            VALUES (?, ?, 'PUBLISHED', ?, ?, 5, ?, ?)
            """,
            (
                sku,
                f"Product {sku}",
                f"L-{sku}",
                json.dumps(["https://img/1.jpg", "https://img/2.jpg"]),
                published_at,
                published_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO cro_snapshots
            (snapshot_date, sku, listing_id, impressions, views, transactions, sold_qty)
            VALUES ('2026-05-13', ?, ?, ?, ?, 0, 0)
            """,
            (sku, f"L-{sku}", impressions, views),
        )
        conn.commit()


def _lifecycle_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='cro_listing_lifecycle_actions'"
        ).fetchone()
        if not exists:
            return 0
        return int(
            conn.execute("SELECT COUNT(*) FROM cro_listing_lifecycle_actions").fetchone()[0]
        )


def _pending_delist_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='cro_delist_pending'"
        ).fetchone()
        if not exists:
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM cro_delist_pending").fetchone()[0])


def test_detect_dry_run_writes_report_without_mutating_original_db(tmp_path: Path):
    from scripts import cro_relist_lifecycle as cli

    db = tmp_path / "cro.db"
    out = tmp_path / "detect.json"
    _create_source_tables(db)
    _seed_product(
        db,
        "REVIVE",
        published_at="2026-04-01T00:00:00",
        impressions=10,
        views=2,
    )
    _seed_product(
        db,
        "DELIST",
        published_at="2026-03-01T00:00:00",
        impressions=0,
        views=0,
    )

    report = cli.run_cli(
        [
            "--detect",
            "--dry-run",
            "--db",
            str(db),
            "--snapshot-date",
            "2026-05-13",
            "--limit",
            "20",
            "--out",
            str(out),
        ]
    )

    assert report["applied"] is False
    assert report["detect"]["created"] == 2
    assert report["detect"]["by_action"] == {"final_delist": 1, "revive_relist": 1}
    assert out.exists()
    assert _lifecycle_count(db) == 0


def test_detect_apply_persists_candidates(tmp_path: Path):
    from scripts import cro_relist_lifecycle as cli

    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(
        db,
        "DELIST",
        published_at="2026-03-01T00:00:00",
        impressions=0,
        views=0,
    )

    report = cli.run_cli(
        [
            "--detect",
            "--apply",
            "--db",
            str(db),
            "--snapshot-date",
            "2026-05-13",
        ]
    )

    assert report["applied"] is True
    assert report["detect"]["by_action"] == {"final_delist": 1}
    assert _lifecycle_count(db) == 1


def test_delist_links_dry_run_does_not_persist_tokens(tmp_path: Path):
    from scripts import cro_relist_lifecycle as cli

    db = tmp_path / "cro.db"
    lifecycle.ensure_schema(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES ('OLD-DEAD', 'final_delist', 'candidate', 'P3',
                    'old_dead_link', 1, '2026-05-13T00:00:00+00:00', 'test')
            """
        )
        conn.commit()

    report = cli.run_cli(
        [
            "--delist-links",
            "--dry-run",
            "--db",
            str(db),
            "--base-url",
            "http://x",
        ]
    )

    assert report["applied"] is False
    assert report["delist_links"]["pending_total"] == 1
    assert report["delist_links"]["rows"][0]["sku"] == "OLD-DEAD"
    assert _pending_delist_count(db) == 0
