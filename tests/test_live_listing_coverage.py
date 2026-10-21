"""Live eBay coverage: sync scope, orphans, and PUBLISHED-not-live flags."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from src.plugins.inventory_sync.sync_service import InventorySyncService
from src.services.live_listing_coverage import (
    LiveListing,
    build_coverage_report,
    load_db_listing_rows,
    sync_scope_products,
)
from src.services.live_listing_reconcile import (
    ACTION_MARK_ENDED,
    ACTION_RESTORE_PUBLISHED,
    build_reconcile_report,
    write_reconcile_reports,
)


def _make_db(tmp_path: Path, rows):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            status TEXT,
            listing_id TEXT,
            title TEXT,
            url TEXT,
            cost_breakdown TEXT,
            optimization TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO collected_products
        (sku, status, listing_id, title, url, cost_breakdown, optimization)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _row(sku, status, listing_id, optimization=None):
    return (
        sku,
        status,
        listing_id,
        f"title-{sku}",
        "",
        json.dumps({"base_cost": 10}),
        json.dumps(optimization) if optimization else None,
    )


def test_live_ended_row_is_covered_and_orphan_is_logged(tmp_path, caplog):
    db = _make_db(tmp_path, [
        _row("W465P220504", "ENDED", "366701946479"),
        _row("PUB-LIVE", "PUBLISHED", "111"),
    ])
    live = [
        LiveListing(item_id="366701946479", sku="W465P220504"),
        LiveListing(item_id="111", sku="PUB-LIVE"),
        LiveListing(item_id="orphan-item", sku="ORPHAN-SKU"),
    ]
    caplog.set_level(logging.WARNING)
    report = build_coverage_report(
        live,
        load_db_listing_rows(db),
        fetch_complete=True,
    )

    covered_skus = {match.sku for match in report.covered}
    assert covered_skus == {"W465P220504", "PUB-LIVE"}
    assert [item.sku for item in report.orphans] == ["ORPHAN-SKU"]
    assert report.published_not_live == []
    assert "LIVE_ORPHAN sku=ORPHAN-SKU item_id=orphan-item" in caplog.text
    assert "W465P220504" in {p["sku"] for p in sync_scope_products(report)}


def test_db_published_but_ended_on_ebay_is_flagged_not_zeroed(tmp_path, caplog):
    db = _make_db(tmp_path, [
        _row("DEAD-PUB", "PUBLISHED", "999"),
        _row("LIVE", "PUBLISHED", "111"),
    ])
    live = [LiveListing(item_id="111", sku="LIVE")]
    caplog.set_level(logging.WARNING)
    report = build_coverage_report(
        live,
        load_db_listing_rows(db),
        fetch_complete=True,
    )

    assert [row.sku for row in report.published_not_live] == ["DEAD-PUB"]
    assert {p["sku"] for p in sync_scope_products(report)} == {"LIVE"}
    assert "PUBLISHED_NOT_LIVE sku=DEAD-PUB" in caplog.text


def test_sync_all_covers_live_ended_logs_orphan_and_does_not_zero_ended_ebay(
    tmp_path, caplog, monkeypatch
):
    db = _make_db(tmp_path, [
        _row("W465P220504", "ENDED", "366701946479"),
        _row("DEAD-PUB", "PUBLISHED", "999"),
    ])
    service = InventorySyncService(db_path=str(db))
    service._load_favorites_set = lambda: set()
    service._prefetch_dajian_data = lambda skus: None
    service.record_sync = lambda result: None
    zeroed = []

    def fake_sync_single(product, dry_run, skip_ebay_check=True, favorites_set=None):
        from src.plugins.inventory_sync.sync_service import SyncResult

        zeroed.append(product["sku"])
        return SyncResult(sku=product["sku"], action="out_of_stock", message="zeroed")

    service._sync_single_product = fake_sync_single
    live = [
        LiveListing(item_id="366701946479", sku="W465P220504"),
        LiveListing(item_id="orphan-item", sku="MISSING-SKU"),
    ]
    caplog.set_level(logging.WARNING)

    results = service.sync_all(
        dry_run=False,
        skip_ebay_check=True,
        skip_synced_today=False,
        live_listings=live,
        fetch_complete=True,
    )

    by_action = {row.action: row for row in results}
    assert "W465P220504" in zeroed
    assert "DEAD-PUB" not in zeroed
    assert by_action["ebay_ended_flagged"].sku == "DEAD-PUB"
    assert "skipped quantity write" in by_action["ebay_ended_flagged"].message
    assert service.last_sync_scope_count == 1
    assert "LIVE_ORPHAN sku=MISSING-SKU" in caplog.text


def test_reconcile_restores_live_ended_and_skips_category_payload(tmp_path):
    lock = {
        "categoryId": "20748",
        "categoryApproved": True,
        "categoryApprovedId": "20748",
        "title": "keep me",
    }
    db = _make_db(tmp_path, [
        _row("W465P220504", "ENDED", "old-id", lock),
        _row("DEAD-PUB", "PUBLISHED", "999"),
    ])
    live = [
        LiveListing(item_id="366701946479", sku="W465P220504"),
        LiveListing(item_id="orphan-item", sku="ORPHAN-SKU"),
    ]
    report = build_reconcile_report(
        db,
        live,
        fetch_complete=True,
        apply=False,
    )
    actions = {row.sku: row.action for row in report.changes}
    assert actions["W465P220504"] == ACTION_RESTORE_PUBLISHED
    assert actions["DEAD-PUB"] == ACTION_MARK_ENDED
    assert report.orphans[0]["sku"] == "ORPHAN-SKU"
    assert all(not row.applied for row in report.changes)

    applied = build_reconcile_report(
        db,
        live,
        fetch_complete=True,
        apply=True,
    )
    assert any(row.applied and row.sku == "W465P220504" for row in applied.changes)

    conn = sqlite3.connect(str(db))
    ended_row = conn.execute(
        "SELECT status, listing_id, optimization FROM collected_products WHERE sku=?",
        ("W465P220504",),
    ).fetchone()
    dead_row = conn.execute(
        "SELECT status FROM collected_products WHERE sku=?",
        ("DEAD-PUB",),
    ).fetchone()
    conn.close()
    assert ended_row[0] == "PUBLISHED"
    assert ended_row[1] == "366701946479"
    assert json.loads(ended_row[2]) == lock
    assert dead_row[0] == "ENDED"

    json_path, csv_path = write_reconcile_reports(applied, tmp_path / "recon")
    assert json_path.exists()
    assert csv_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["orphan_count"] == 1
    assert "ORPHAN-SKU" in csv_path.read_text(encoding="utf-8")


def test_incomplete_fetch_does_not_mark_published_ended(tmp_path):
    db = _make_db(tmp_path, [
        _row("MAYBE-LIVE", "PUBLISHED", "888"),
        _row("ENDED-LIVE", "ENDED", "777"),
    ])
    live = [LiveListing(item_id="777", sku="ENDED-LIVE")]
    report = build_reconcile_report(
        db,
        live,
        fetch_complete=False,
        apply=True,
    )
    actions = {row.sku: row.action for row in report.changes}
    assert actions["ENDED-LIVE"] == ACTION_RESTORE_PUBLISHED
    assert "MAYBE-LIVE" not in actions

    conn = sqlite3.connect(str(db))
    assert conn.execute(
        "SELECT status FROM collected_products WHERE sku='MAYBE-LIVE'"
    ).fetchone()[0] == "PUBLISHED"
    assert conn.execute(
        "SELECT status FROM collected_products WHERE sku='ENDED-LIVE'"
    ).fetchone()[0] == "PUBLISHED"
    conn.close()
