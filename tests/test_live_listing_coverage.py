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


def test_short_active_list_keeps_published_rows_in_stock_gate(tmp_path, monkeypatch):
    """A truncated ActiveList must not look finished.

    Unseen PUBLISHED rows stay in the qty=0 gate (zero_only) instead of being
    flagged as ended and dropped.
    """
    db = _make_db(tmp_path, [
        _row("LIVE", "PUBLISHED", "111"),
        _row("UNSEEN", "PUBLISHED", "999"),
    ])
    monkeypatch.setattr(
        "src.services.listing_status_sync.fetch_all_active_listings",
        lambda trading=None, max_pages=20: (
            [LiveListing(item_id="111", sku="LIVE")],
            40,
            True,
        ),
    )
    service = InventorySyncService(db_path=str(db))
    service._load_favorites_set = lambda: set()
    service._prefetch_dajian_data = lambda skus: None
    service.record_sync = lambda result: None
    seen = []

    def fake_sync_single(product, dry_run, skip_ebay_check=True, favorites_set=None):
        from src.plugins.inventory_sync.sync_service import SyncResult

        seen.append((product["sku"], product.get("sync_mode")))
        return SyncResult(sku=product["sku"], action="out_of_stock", message="zeroed")

    service._sync_single_product = fake_sync_single
    results = service.sync_all(
        dry_run=False,
        skip_ebay_check=True,
        skip_synced_today=False,
    )

    assert ("LIVE", None) in seen
    assert ("UNSEEN", "zero_only") in seen
    assert all(row.action != "ebay_ended_flagged" for row in results)
    assert service.last_coverage_report is not None
    assert service.last_coverage_report.fetch_complete is False


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
        ended_confirmer=lambda sku, listing_id: True,
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


SPEC_ORPHAN_SKUS = (
    "W2932P435459",
    "W2519S00001",
    "W2932P000003",
    "W2932P000004",
    "W2519S00005",
    "W2519S00006",
    "W2932P000007",
)


def test_spec_skus_ended_ready_and_seven_orphans(tmp_path, caplog):
    """2026-10-22 scan cases: live+ENDED, READY+live, 7 live orphans."""
    db = _make_db(tmp_path, [
        _row("W465P220504", "ENDED", "366701946479"),
        _row("W465P229056", "READY", "ready-old-id"),
        _row("DEAD-PUB", "PUBLISHED", "ended-on-ebay"),
    ])
    live = [
        LiveListing(item_id="366701946479", sku="W465P220504"),
        LiveListing(item_id="229056111111", sku="W465P229056"),
        *[
            LiveListing(item_id=f"orphan-{index}", sku=sku)
            for index, sku in enumerate(SPEC_ORPHAN_SKUS, start=1)
        ],
    ]
    caplog.set_level(logging.WARNING)
    report = build_coverage_report(live, load_db_listing_rows(db), fetch_complete=True)
    covered = {match.sku: match for match in report.covered}

    assert "W465P220504" in covered
    assert covered["W465P220504"].db_status == "ENDED"
    assert "W465P229056" in covered
    assert covered["W465P229056"].db_status == "READY"
    assert {item.sku for item in report.orphans} == set(SPEC_ORPHAN_SKUS)
    assert [row.sku for row in report.published_not_live] == ["DEAD-PUB"]
    assert "W465P220504" in {p["sku"] for p in sync_scope_products(report)}
    assert "W465P229056" in {p["sku"] for p in sync_scope_products(report)}
    assert "DEAD-PUB" not in {p["sku"] for p in sync_scope_products(report)}
    assert "W2932P435459" in caplog.text
    assert "W2519S00001" in caplog.text

    planned = build_reconcile_report(db, live, fetch_complete=True, apply=False)
    by_sku = {row.sku: row for row in planned.changes}
    assert by_sku["W465P220504"].action == ACTION_RESTORE_PUBLISHED
    assert by_sku["W465P229056"].action == ACTION_RESTORE_PUBLISHED
    assert by_sku["W465P229056"].db_listing_id_after == "229056111111"
    assert {row["sku"] for row in planned.orphans} == set(SPEC_ORPHAN_SKUS)
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM collected_products").fetchone()[0] == 3
    conn.close()


def test_orphan_sku_does_not_bind_to_colliding_listing_id(tmp_path, caplog):
    db = _make_db(tmp_path, [
        _row("OTHER-SKU", "ENDED", "shared-item"),
    ])
    live = [LiveListing(item_id="shared-item", sku="W2932P435459")]
    caplog.set_level(logging.WARNING)
    report = build_coverage_report(live, load_db_listing_rows(db), fetch_complete=True)

    assert report.covered == []
    assert [item.sku for item in report.orphans] == ["W2932P435459"]
    assert "LIVE_ORPHAN sku=W2932P435459" in caplog.text
    recon = build_reconcile_report(db, live, fetch_complete=True, apply=True)
    assert recon.changes == []
    conn = sqlite3.connect(str(db))
    assert conn.execute(
        "SELECT status, listing_id FROM collected_products WHERE sku='OTHER-SKU'"
    ).fetchone() == ("ENDED", "shared-item")
    assert conn.execute(
        "SELECT 1 FROM collected_products WHERE sku='W2932P435459'"
    ).fetchone() is None
    conn.close()


def test_null_listing_id_still_matches_sku_and_reconcile_fills_it(tmp_path):
    db = _make_db(tmp_path, [
        _row("W465P220504", "ENDED", None),
    ])
    live = [LiveListing(item_id="366701946479", sku="W465P220504")]
    report = build_coverage_report(live, load_db_listing_rows(db), fetch_complete=True)
    assert report.covered[0].sku == "W465P220504"
    assert report.covered[0].match_by == "sku"

    build_reconcile_report(db, live, fetch_complete=True, apply=True)
    conn = sqlite3.connect(str(db))
    assert conn.execute(
        "SELECT status, listing_id FROM collected_products WHERE sku='W465P220504'"
    ).fetchone() == ("PUBLISHED", "366701946479")
    conn.close()


def test_invalid_cost_breakdown_and_null_optimization_still_cover(tmp_path):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE collected_products "
        "(sku TEXT PRIMARY KEY, status TEXT, listing_id TEXT, "
        "title TEXT, url TEXT, cost_breakdown TEXT, optimization TEXT)"
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?,?,?,?,?,?,?)",
        ("W465P220504", "ENDED", "366701946479", "t", "", "not-json", None),
    )
    conn.commit()
    conn.close()
    report = build_coverage_report(
        [LiveListing(item_id="366701946479", sku="W465P220504")],
        load_db_listing_rows(db),
        fetch_complete=True,
    )
    assert report.covered[0].product["cost_breakdown"] == {}
    assert report.covered[0].sku == "W465P220504"


def test_mark_ended_apply_requires_ebay_confirmation(tmp_path):
    db = _make_db(tmp_path, [
        _row("DEAD-PUB", "PUBLISHED", "999"),
    ])
    report = build_reconcile_report(
        db,
        [],
        fetch_complete=True,
        apply=True,
        ended_confirmer=lambda sku, listing_id: False,
    )
    assert report.changes[0].action == ACTION_MARK_ENDED
    assert report.changes[0].applied is False
    conn = sqlite3.connect(str(db))
    assert conn.execute(
        "SELECT status FROM collected_products WHERE sku='DEAD-PUB'"
    ).fetchone()[0] == "PUBLISHED"
    conn.close()
