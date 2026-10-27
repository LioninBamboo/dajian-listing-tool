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
    # Missing cache must not be read, and must not count as a complete report.
    monkeypatch.setattr(
        "src.services.ebay_performance.load_performance_cache",
        lambda max_age_hours=72: None,
    )


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
    # Positive snapshot still revives. The zero row is unverified, so it is not an End.
    assert report["detect"]["created"] == 1
    assert report["detect"]["by_action"] == {"revive_relist": 1}
    assert report["detect"]["skipped"]["traffic_unverified_for_end"] == 1
    assert out.exists()
    assert _lifecycle_count(db) == 0


def test_detect_apply_skips_unverified_zero_without_insert(tmp_path: Path):
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
    assert report["detect"]["created"] == 0
    assert report["detect"]["by_action"] == {}
    assert report["detect"]["skipped"]["traffic_unverified_for_end"] == 1
    assert _lifecycle_count(db) == 0


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


def test_execute_cli_flag_off_exits_nonzero_without_ebay(tmp_path: Path, monkeypatch, capsys):
    from scripts import cro_relist_lifecycle as cli

    db = tmp_path / "cro.db"
    lifecycle.ensure_schema(db)

    def boom(*_args, **_kwargs):
        raise AssertionError("eBay must not be called")

    monkeypatch.setattr(lifecycle, "_cro_relist_enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "_fetch_store_active_listings", boom)
    monkeypatch.setattr(lifecycle, "_default_trading_client", boom)
    monkeypatch.setattr(lifecycle, "_lookup_listing_traffic_live", boom)

    code = cli.main(["--execute", "--apply", "--db", str(db), "--limit", "5"])
    captured = capsys.readouterr()

    assert code == 2
    assert "scheduler_enable_cro_relist" in captured.err
    assert "scheduler_enable_cro_relist" in captured.out


def test_detect_and_evaluate_do_not_touch_ebay(tmp_path: Path, monkeypatch):
    from scripts import cro_relist_lifecycle as cli

    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(
        db,
        "REVIVE",
        published_at="2026-04-01T00:00:00",
        impressions=10,
        views=2,
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("eBay must not be called")

    monkeypatch.setattr(lifecycle, "_fetch_store_active_listings", boom)
    monkeypatch.setattr(lifecycle, "_default_trading_client", boom)
    monkeypatch.setattr(lifecycle, "_lookup_listing_traffic_live", boom)
    monkeypatch.setattr(
        "src.services.listing_status_sync.fetch_all_active_listings",
        boom,
    )
    monkeypatch.setattr(
        "src.services.ebay_performance.EbayPerformanceService",
        boom,
    )

    detect_report = cli.run_cli(
        ["--detect", "--dry-run", "--db", str(db), "--snapshot-date", "2026-05-13"]
    )
    evaluate_report = cli.run_cli(
        ["--evaluate", "--dry-run", "--db", str(db), "--snapshot-date", "2026-05-13"]
    )

    assert detect_report["mode"] == "detect"
    assert evaluate_report["mode"] == "evaluate"
    assert detect_report.get("refused") is not True
