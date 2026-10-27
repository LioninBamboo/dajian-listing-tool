"""CRO relist lifecycle foundation tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import src.services.cro_relist_lifecycle as lifecycle
from src.services.cro_relist_lifecycle import (
    approve_actions,
    detect_candidates,
    ensure_schema,
    execute_prechecked_relist,
    precheck_approved,
    transition_action,
)


@pytest.fixture(autouse=True)
def _isolate_mi_blacklist(monkeypatch):
    monkeypatch.setattr(lifecycle, "_load_mi_blacklist", lambda path=None: set())


# Shape of Main's 2026-10-23 truncated top-200 Traffic Report cache.
_TRUNCATED_TRAFFIC_META = {
    "api_ok": True,
    "is_truncated": True,
    "record_count": 200,
    "limit": 200,
}

_REAL_LOAD_PERFORMANCE_TRAFFIC_INDEX = lifecycle._load_performance_traffic_index


def _truncated_performance_index():
    return {}, dict(_TRUNCATED_TRAFFIC_META)


@pytest.fixture(autouse=True)
def _hermetic_truncated_performance_cache(monkeypatch):
    """Never read cache/performance_cache.json. Default to a truncated top-200."""
    monkeypatch.setattr(
        lifecycle,
        "_load_performance_traffic_index",
        _truncated_performance_index,
    )


def _use_real_performance_cache(monkeypatch, payload):
    """Run the real loader against an injected cache payload, not the runtime file."""
    monkeypatch.setattr(
        lifecycle,
        "_load_performance_traffic_index",
        _REAL_LOAD_PERFORMANCE_TRAFFIC_INDEX,
    )

    def _load(max_age_hours=72):
        return payload

    monkeypatch.setattr("src.services.ebay_performance.load_performance_cache", _load)


def _create_source_tables(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE collected_products (
                sku TEXT PRIMARY KEY,
                title TEXT,
                price REAL,
                suggested_price REAL,
                status TEXT,
                listing_id TEXT,
                images TEXT,
                videos TEXT,
                attributes TEXT,
                specs TEXT,
                cost_breakdown TEXT,
                optimization TEXT,
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
    status: str = "PUBLISHED",
    listing_id: str = "L-1",
    images: list[str] | None = None,
    stock: int = 5,
    price: float = 100.0,
    suggested_price: float = 150.0,
    published_at: str | None = "2026-04-01T00:00:00",
    created_at: str = "2026-04-01T00:00:00",
) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO collected_products
            (
                sku, title, price, suggested_price, status, listing_id, images,
                videos, attributes, specs, cost_breakdown, optimization,
                stock, published_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sku,
                f"Product {sku}",
                price,
                suggested_price,
                status,
                listing_id,
                json.dumps(images or ["https://img/1.jpg", "https://img/2.jpg"]),
                json.dumps([]),
                json.dumps({"Material": "wood"}),
                json.dumps({"Package Weight (lbs.)": "12"}),
                json.dumps({"total_dajian_cost": 50}),
                json.dumps({
                    "title": f"Product {sku}",
                    "description": "Long enough listing description",
                    "categoryId": "123",
                    "aspects": {"Brand": ["Unbranded"]},
                }),
                stock,
                published_at,
                created_at,
            ),
        )
        conn.commit()


def _seed_snapshot(
    db_path: Path,
    sku: str,
    *,
    snapshot_date: str = "2026-05-13",
    listing_id: str = "L-1",
    impressions: int = 25,
    views: int = 3,
    transactions: int = 0,
    sold_qty: int = 0,
) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO cro_snapshots
            (snapshot_date, sku, listing_id, impressions, views, transactions, sold_qty)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (snapshot_date, sku, listing_id, impressions, views, transactions, sold_qty),
        )
        conn.commit()


class _LiveListing:
    def __init__(self, item_id: str, sku: str):
        self.item_id = item_id
        self.sku = sku


def _enable_relist_with_activelist(monkeypatch, listings):
    """Flag on, and a complete ActiveList that does not hit eBay."""
    rows = list(listings)
    monkeypatch.setattr(lifecycle, "_cro_relist_enabled", lambda: True)
    monkeypatch.setattr(
        lifecycle,
        "_fetch_store_active_listings",
        lambda trading=None: (rows, len(rows), True),
    )


def _actions(db_path: Path) -> list[dict]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM cro_listing_lifecycle_actions ORDER BY id"
            )
        ]


def _verified_zero_traffic(listing_id: str):
    return {"impressions": 0, "views": 0, "transactions": 0, "verified": True, "source": "test"}


def test_detect_candidates_creates_revive_action_for_30_day_no_sale(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-A")
    _seed_snapshot(db, "SKU-A", impressions=42, views=7)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["created"] == 1
    rows = _actions(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["sku"] == "SKU-A"
    assert row["action_type"] == "revive_relist"
    assert row["status"] == "candidate"
    assert row["priority"] == "P2"
    assert row["old_listing_id"] == "L-1"
    assert row["attempt_no"] == 1
    metrics = json.loads(row["metrics_before_json"])
    assert metrics["age_days"] == 42
    assert metrics["age_source"] == "published_at"
    assert metrics["sales_detected"] is False


def test_detect_candidates_uses_created_at_fallback_and_zero_impression_priority(
    tmp_path: Path,
):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-ZERO", published_at=None)
    _seed_snapshot(db, "SKU-ZERO", impressions=0, views=0)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["priority"] == "P3"
    metrics = json.loads(row["metrics_before_json"])
    assert metrics["age_source"] == "created_at_fallback"
    assert metrics["impressions"] == 0
    assert metrics["views"] == 0


def test_detect_candidates_skips_rows_with_any_sales(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-SOLD")
    _seed_snapshot(db, "SKU-SOLD", transactions=1, sold_qty=0)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["created"] == 0
    assert result["skipped"]["sales_present"] == 1
    assert _actions(db) == []


def test_detect_candidates_routes_old_zero_traffic_dead_link_to_final_delist(
    tmp_path: Path,
):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-DEAD", published_at="2026-03-01T00:00:00")
    _seed_snapshot(db, "SKU-DEAD", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=_verified_zero_traffic,
    )

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["sku"] == "SKU-DEAD"
    assert row["action_type"] == "final_delist"
    assert row["status"] == "candidate"
    assert row["priority"] == "P3"
    assert "old_dead_link" in row["reason"]
    metrics = json.loads(row["metrics_before_json"])
    assert metrics["age_days"] == 73
    assert metrics["impressions"] == 0
    assert metrics["views"] == 0


def test_detect_candidates_old_dead_link_does_not_need_revive_image_gate(
    tmp_path: Path,
):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(
        db,
        "SKU-DEAD-IMG",
        images=["https://img/only.jpg"],
        published_at="2026-03-01T00:00:00",
    )
    _seed_snapshot(db, "SKU-DEAD-IMG", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=_verified_zero_traffic,
    )

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "final_delist"
    assert row["sku"] == "SKU-DEAD-IMG"


def test_detect_candidates_prevents_duplicate_pending_action(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-DUP")
    _seed_snapshot(db, "SKU-DUP")

    first = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    second = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["skipped"]["duplicate_active_action"] == 1
    assert len(_actions(db)) == 1


def test_detect_candidates_blocks_after_one_relist_attempt(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    ensure_schema(db)
    _seed_product(db, "SKU-ONCE")
    _seed_snapshot(db, "SKU-ONCE")
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, reason, attempt_no, created_at, source)
            VALUES (?, 'revive_relist', 'revived_success', 'P2', 'prior', 1, ?, 'test')
            """,
            ("SKU-ONCE", "2026-05-01T00:00:00+00:00"),
        )
        conn.commit()

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["created"] == 0
    assert result["skipped"]["relist_attempt_limit"] == 1


def test_approve_actions_uses_allowed_transition(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-APPROVE")
    _seed_snapshot(db, "SKU-APPROVE")
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]

    result = approve_actions([action_id], operator="ops", db_path=db)

    assert result == {"approved": 1, "skipped": 0, "missing": 0}
    row = _actions(db)[0]
    assert row["status"] == "approved"
    assert row["approved_by"] == "ops"
    assert row["approved_at"]


def test_precheck_approved_moves_safe_rows_to_prechecked(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-PRECHECK", stock=4)
    _seed_snapshot(db, "SKU-PRECHECK", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]
    approve_actions([action_id], operator="ops", db_path=db)

    result = precheck_approved(limit=10, apply_changes=True, db_path=db)

    assert result["prechecked"] == 1
    assert result["skipped"] == 0
    row = _actions(db)[0]
    assert row["status"] == "prechecked"
    assert row["started_at"]
    assert json.loads(row["db_snapshot_json"])["product"]["sku"] == "SKU-PRECHECK"
    assert json.loads(row["live_snapshot_json"])["source"] == "db_precheck"


def test_precheck_approved_skips_rows_with_new_sales(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-SALE")
    _seed_snapshot(db, "SKU-SALE", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]
    approve_actions([action_id], operator="ops", db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE cro_snapshots SET transactions = 1 WHERE sku = 'SKU-SALE'"
        )
        conn.commit()

    result = precheck_approved(limit=10, apply_changes=True, db_path=db)

    assert result["prechecked"] == 0
    assert result["skipped"] == 1
    row = _actions(db)[0]
    assert row["status"] == "skipped"
    assert "sales_present" in row["error"]


def test_execute_prechecked_relist_withdraws_and_publishes(tmp_path: Path, monkeypatch):
    _enable_relist_with_activelist(
        monkeypatch, [_LiveListing("OTHER-LIVE", "OTHER-SKU")]
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-RELIVE", listing_id="OLD-1")
    _seed_snapshot(db, "SKU-RELIVE", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]
    approve_actions([action_id], operator="ops", db_path=db)
    precheck_approved(limit=1, apply_changes=True, db_path=db)

    class FakeTrading:
        calls = []

        def end_item(self, item_id, reason="NotAvailable"):
            self.calls.append((item_id, reason))
            return (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<EndFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
                '<Ack>Success</Ack>'
                '</EndFixedPriceItemResponse>'
            )

    def dry_run(product):
        assert isinstance(product["images"], list)
        assert isinstance(product["attributes"], dict)
        assert isinstance(product["specs"], dict)
        assert isinstance(product["cost_breakdown"], dict)
        assert isinstance(product["optimization"], dict)
        return {"status": "dry_run", "message": "ok"}

    def publish(product):
        assert isinstance(product["images"], list)
        assert isinstance(product["optimization"], dict)
        return {
            "status": "success",
            "listing_id": "NEW-1",
            "offer_id": "OFFER-1",
            "message": "published",
        }

    trading = FakeTrading()
    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=trading,
        dry_run_publish_func=dry_run,
        publish_func=publish,
    )

    assert result["published_new"] == 1
    assert trading.calls == [("OLD-1", "NotAvailable")]
    row = _actions(db)[0]
    assert row["status"] == "published_new"
    assert row["new_listing_id"] == "NEW-1"
    assert row["new_offer_id"] == "OFFER-1"
    with sqlite3.connect(str(db)) as conn:
        product_status, listing_id = conn.execute(
            "SELECT status, listing_id FROM collected_products WHERE sku='SKU-RELIVE'"
        ).fetchone()
    assert product_status == "PUBLISHED"
    assert listing_id == "NEW-1"


def test_execute_prechecked_relist_dry_run_failure_keeps_old_listing(tmp_path: Path, monkeypatch):
    _enable_relist_with_activelist(
        monkeypatch, [_LiveListing("OTHER-LIVE", "OTHER-SKU")]
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-BLOCK", listing_id="OLD-2")
    _seed_snapshot(db, "SKU-BLOCK", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]
    approve_actions([action_id], operator="ops", db_path=db)
    precheck_approved(limit=1, apply_changes=True, db_path=db)

    class FakeTrading:
        def end_item(self, item_id, reason="NotAvailable"):
            raise AssertionError("old listing should not be withdrawn")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=FakeTrading(),
        dry_run_publish_func=lambda product: {"status": "error", "message": "blocked"},
        publish_func=lambda product: {"status": "success", "listing_id": "NEW-2"},
    )

    assert result["skipped"] == 1
    row = _actions(db)[0]
    assert row["status"] == "skipped"
    assert "dry_run_failed" in row["error"]
    with sqlite3.connect(str(db)) as conn:
        product_status, listing_id = conn.execute(
            "SELECT status, listing_id FROM collected_products WHERE sku='SKU-BLOCK'"
        ).fetchone()
    assert product_status == "PUBLISHED"
    assert listing_id == "OLD-2"


def test_execute_prechecked_relist_skips_when_sales_arrive_after_precheck(tmp_path: Path, monkeypatch):
    _enable_relist_with_activelist(
        monkeypatch, [_LiveListing("OTHER-LIVE", "OTHER-SKU")]
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-SALE-LATE", listing_id="OLD-3")
    _seed_snapshot(db, "SKU-SALE-LATE", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]
    approve_actions([action_id], operator="ops", db_path=db)
    precheck_approved(limit=1, apply_changes=True, db_path=db)

    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE cro_snapshots SET transactions = 1, sold_qty = 1 WHERE sku = 'SKU-SALE-LATE'"
        )
        conn.commit()

    class FakeTrading:
        def end_item(self, item_id, reason="NotAvailable"):
            raise AssertionError("sold listing should not be withdrawn after sales recheck")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=FakeTrading(),
        dry_run_publish_func=lambda product: {"status": "dry_run", "message": "ok"},
        publish_func=lambda product: {"status": "success", "listing_id": "NEW-3"},
    )

    assert result["published_new"] == 0
    assert result["skipped"] == 1
    row = _actions(db)[0]
    assert row["status"] == "skipped"
    assert "sales_present" in row["error"]
    with sqlite3.connect(str(db)) as conn:
        product_status, listing_id = conn.execute(
            "SELECT status, listing_id FROM collected_products WHERE sku='SKU-SALE-LATE'"
        ).fetchone()
    assert product_status == "PUBLISHED"
    assert listing_id == "OLD-3"


def _precheck_one(db: Path, sku: str = "SKU-RELIVE", listing_id: str = "OLD-1") -> int:
    _create_source_tables(db)
    _seed_product(db, sku, listing_id=listing_id)
    _seed_snapshot(db, sku, listing_id=listing_id, impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]
    approve_actions([action_id], operator="ops", db_path=db)
    precheck_approved(limit=1, apply_changes=True, db_path=db)
    return action_id


def test_execute_flag_off_refuses_with_zero_ebay_calls(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)

    def boom(*_args, **_kwargs):
        raise AssertionError("eBay must not be called when relist flag is off")

    monkeypatch.setattr(lifecycle, "_cro_relist_enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "_fetch_store_active_listings", boom)
    monkeypatch.setattr(lifecycle, "_default_trading_client", boom)
    monkeypatch.setattr(lifecycle, "_default_publish", boom)
    monkeypatch.setattr(lifecycle, "_default_dry_run_publish", boom)

    class FakeTrading:
        def end_item(self, item_id, reason="NotAvailable"):
            raise AssertionError("end_item")

        def call(self, *args, **kwargs):
            raise AssertionError("trading.call")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=FakeTrading(),
        dry_run_publish_func=boom,
        publish_func=boom,
    )

    assert result["refused"] is True
    assert result["ebay_calls"] == 0
    assert "scheduler_enable_cro_relist" in result["message"]
    assert _actions(db)[0]["status"] == "prechecked"


def test_execute_refuses_still_live_sku(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db, sku="SKU-LIVE", listing_id="LIVE-1")
    _enable_relist_with_activelist(
        monkeypatch, [_LiveListing("LIVE-1", "SKU-LIVE")]
    )

    class FakeTrading:
        def end_item(self, item_id, reason="NotAvailable"):
            raise AssertionError("live listing must not be ended")

        def call(self, *args, **kwargs):
            raise AssertionError("trading.call")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=FakeTrading(),
        dry_run_publish_func=lambda product: (_ for _ in ()).throw(AssertionError("dry_run")),
        publish_func=lambda product: (_ for _ in ()).throw(AssertionError("publish")),
    )

    assert result.get("aborted") is not True
    assert result["skipped"] == 1
    assert result["results"][0]["reason"] == "still_live"
    row = _actions(db)[0]
    assert row["status"] == "skipped"
    assert row["error"] == "still_live"


def test_execute_aborts_when_activelist_fails(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)

    def boom(*_args, **_kwargs):
        raise RuntimeError("GetMyeBaySelling down")

    monkeypatch.setattr(lifecycle, "_cro_relist_enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "_fetch_store_active_listings", boom)

    class FakeTrading:
        def end_item(self, item_id, reason="NotAvailable"):
            raise AssertionError("end_item")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=FakeTrading(),
        publish_func=lambda product: (_ for _ in ()).throw(AssertionError("publish")),
    )

    assert result["aborted"] is True
    assert result["reason"] == "activelist_fetch_failed"
    assert result["results"] == []
    assert _actions(db)[0]["status"] == "prechecked"


def test_execute_aborts_when_activelist_empty_but_db_has_live(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)
    _enable_relist_with_activelist(monkeypatch, [])

    class FakeTrading:
        def end_item(self, item_id, reason="NotAvailable"):
            raise AssertionError("end_item")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=FakeTrading(),
        publish_func=lambda product: (_ for _ in ()).throw(AssertionError("publish")),
    )

    assert result["aborted"] is True
    assert result["reason"] == "activelist_empty_with_db_live"
    assert result["results"] == []
    assert _actions(db)[0]["status"] == "prechecked"


def _selling_page(items, total_entries, total_pages, ack="Success"):
    body = "".join(
        f"<Item><ItemID>{iid}</ItemID><SKU>{sku}</SKU></Item>"
        for iid, sku in items
    )
    return (
        '<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<Ack>{ack}</Ack>"
        "<ActiveList><PaginationResult>"
        f"<TotalNumberOfEntries>{total_entries}</TotalNumberOfEntries>"
        f"<TotalNumberOfPages>{total_pages}</TotalNumberOfPages>"
        "</PaginationResult>"
        f"<ItemArray>{body}</ItemArray></ActiveList>"
        "</GetMyeBaySellingResponse>"
    )


class _PagingTrading:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[str] = []
        self.ends: list[str] = []

    def call(self, verb, payload):
        self.calls.append(verb)
        if not self.pages:
            return None
        return self.pages.pop(0)

    def end_item(self, item_id, reason="NotAvailable"):
        self.ends.append(item_id)
        raise AssertionError("end_item")


def _execute_pages(db, monkeypatch, pages):
    monkeypatch.setattr(lifecycle, "_cro_relist_enabled", lambda: True)
    monkeypatch.setattr(
        "src.services.listing_status_sync.time.sleep",
        lambda *_args, **_kwargs: None,
    )
    trading = _PagingTrading(pages)

    def boom(_product):
        raise AssertionError("publish")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=trading,
        dry_run_publish_func=boom,
        publish_func=boom,
    )
    return result, trading


def test_execute_aborts_when_activelist_ack_is_not_success(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)
    result, trading = _execute_pages(
        db, monkeypatch, [_selling_page([("LIVE-9", "OTHER")], 1, 1, ack="Failure")]
    )
    assert result["aborted"] is True
    assert result["reason"] == "activelist_fetch_failed"
    assert result["ebay_calls"] == 0
    assert trading.ends == []
    assert _actions(db)[0]["status"] == "prechecked"


def test_execute_aborts_when_api_total_exceeds_rows(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)
    result, trading = _execute_pages(
        db, monkeypatch, [_selling_page([("LIVE-9", "OTHER")], 10, 1)]
    )
    assert result["aborted"] is True
    assert result["reason"] == "activelist_fetch_failed"
    assert "api_total=10" in result["message"]
    assert trading.ends == []
    assert _actions(db)[0]["status"] == "prechecked"


def test_execute_aborts_when_short_total_even_if_flagged_complete(tmp_path: Path, monkeypatch):
    """Caller must not discard api_total when the fetcher says complete."""
    db = tmp_path / "cro.db"
    _precheck_one(db)
    monkeypatch.setattr(lifecycle, "_cro_relist_enabled", lambda: True)
    monkeypatch.setattr(
        lifecycle,
        "_fetch_store_active_listings",
        lambda trading=None: ([_LiveListing("OTHER", "OTHER")], 10, True),
    )
    trading = _PagingTrading([])
    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=trading,
        publish_func=lambda product: (_ for _ in ()).throw(AssertionError("publish")),
    )
    assert result["aborted"] is True
    assert result["reason"] == "activelist_fetch_failed"
    assert trading.ends == []
    assert _actions(db)[0]["status"] == "prechecked"


def test_execute_aborts_when_pagination_ends_early(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)
    result, trading = _execute_pages(
        db,
        monkeypatch,
        [
            _selling_page([("A", "OTHER-A"), ("B", "OTHER-B")], 5, 3),
            _selling_page([], 5, 3),
        ],
    )
    assert result["aborted"] is True
    assert result["reason"] == "activelist_fetch_failed"
    assert trading.ends == []
    assert len(trading.calls) == 2
    assert _actions(db)[0]["status"] == "prechecked"


def test_execute_aborts_when_zero_rows_and_db_published_via_api(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)
    result, trading = _execute_pages(
        db, monkeypatch, [_selling_page([], 0, 0)]
    )
    assert result["aborted"] is True
    assert result["reason"] == "activelist_empty_with_db_live"
    assert trading.ends == []
    assert _actions(db)[0]["status"] == "prechecked"


def test_execute_aborts_when_activelist_incomplete(tmp_path: Path, monkeypatch):
    db = tmp_path / "cro.db"
    _precheck_one(db)
    monkeypatch.setattr(lifecycle, "_cro_relist_enabled", lambda: True)
    monkeypatch.setattr(
        lifecycle,
        "_fetch_store_active_listings",
        lambda trading=None: ([_LiveListing("OTHER", "OTHER")], 99, False),
    )

    class FakeTrading:
        def end_item(self, item_id, reason="NotAvailable"):
            raise AssertionError("end_item")

    result = execute_prechecked_relist(
        limit=1,
        apply_changes=True,
        db_path=db,
        trading_client=FakeTrading(),
    )

    assert result["aborted"] is True
    assert result["reason"] == "activelist_fetch_failed"
    assert _actions(db)[0]["status"] == "prechecked"


def test_transition_action_rejects_invalid_status_jump(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-JUMP")
    _seed_snapshot(db, "SKU-JUMP")
    detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")
    action_id = _actions(db)[0]["id"]

    with pytest.raises(ValueError):
        transition_action(action_id, "ready_to_publish", db_path=db)

    assert _actions(db)[0]["status"] == "candidate"

from datetime import date, datetime, timedelta, timezone

def test_evaluate_transitions_published_new_to_observing(tmp_path, monkeypatch):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    lifecycle.ensure_schema(db)
    
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, attempt_no, created_at)
            VALUES ('SKU-1', 'revive_relist', 'published_new', 'P2', 1, ?)
            """,
            ("2026-05-01T00:00:00+00:00",)
        )
        conn.commit()
    
    res = lifecycle.evaluate_observations(db_path=db)
    assert res["started_observing"] == 1
    
    row = _actions(db)[0]
    assert row["status"] == "observing"
    assert row["observe_window_days"] == 14
    assert row["observe_until"]

def test_evaluate_resolves_early_on_sale(tmp_path, monkeypatch):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    lifecycle.ensure_schema(db)
    
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, attempt_no, created_at, observe_until)
            VALUES ('SKU-SALE', 'revive_relist', 'observing', 'P2', 1, ?, ?)
            """,
            ("2026-05-01T00:00:00+00:00", "2026-05-15T00:00:00+00:00")
        )
        conn.commit()
    
    _seed_snapshot(db, "SKU-SALE", transactions=1)
    
    res = lifecycle.evaluate_observations(db_path=db)
    assert res["evaluated"] == 1
    
    row = _actions(db)[0]
    assert row["status"] == "revived_success"

def test_evaluate_waits_for_observe_until(tmp_path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    lifecycle.ensure_schema(db)
    
    future_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, attempt_no, created_at, observe_until)
            VALUES ('SKU-WAIT', 'revive_relist', 'observing', 'P2', 1, ?, ?)
            """,
            ("2026-05-01T00:00:00+00:00", future_date)
        )
        conn.commit()
    
    _seed_snapshot(db, "SKU-WAIT", transactions=0)
    
    res = lifecycle.evaluate_observations(db_path=db)
    assert res["evaluated"] == 0
    row = _actions(db)[0]
    assert row["status"] == "observing"

def test_evaluate_recovers_traffic_triggers_conversion_help(tmp_path, monkeypatch):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    lifecycle.ensure_schema(db)
    
    enqueued = []
    monkeypatch.setattr("src.services.cro_action_queue.enqueue_unique_pending", lambda a, source: enqueued.append(a))
    
    past_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    metrics_before = json.dumps({"impressions": 10, "views": 1})
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, attempt_no, created_at, observe_until, metrics_before_json, conversion_help_attempts)
            VALUES ('SKU-HELP', 'revive_relist', 'observing', 'P2', 1, ?, ?, ?, 0)
            """,
            ("2026-05-01T00:00:00+00:00", past_date, metrics_before)
        )
        conn.commit()
    
    _seed_snapshot(db, "SKU-HELP", impressions=60, views=6, transactions=0)
    
    res = lifecycle.evaluate_observations(db_path=db)
    assert res["evaluated"] == 1
    
    row = _actions(db)[0]
    assert row["status"] == "observing"
    assert row["conversion_help_attempts"] == 1
    assert len(enqueued) == 1
    actions = enqueued[0]
    assert len(actions) == 2
    assert actions[0]["action"] == "promoted_listings"
    assert actions[1]["action"] == "reprice"

def test_evaluate_traffic_dead_triggers_fallback_delist(tmp_path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    lifecycle.ensure_schema(db)
    
    past_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    metrics_before = json.dumps({"impressions": 100, "views": 10})
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, attempt_no, created_at, observe_until, metrics_before_json, conversion_help_attempts)
            VALUES ('SKU-DEAD', 'revive_relist', 'observing', 'P2', 1, ?, ?, ?, 0)
            """,
            ("2026-05-01T00:00:00+00:00", past_date, metrics_before)
        )
        conn.commit()
    
    _seed_snapshot(db, "SKU-DEAD", impressions=10, views=1, transactions=0)
    
    res = lifecycle.evaluate_observations(db_path=db)
    assert res["evaluated"] == 1
    
    row = _actions(db)[0]
    assert row["status"] == "fallback_delist_pending"

def test_evaluate_caps_conversion_help_at_one(tmp_path, monkeypatch):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    lifecycle.ensure_schema(db)
    
    enqueued = []
    monkeypatch.setattr("src.services.cro_action_queue.enqueue_unique_pending", lambda a, source: enqueued.append(a))
    
    past_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    metrics_before = json.dumps({"impressions": 10, "views": 1})
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO cro_listing_lifecycle_actions
            (sku, action_type, status, priority, attempt_no, created_at, observe_until, metrics_before_json, conversion_help_attempts)
            VALUES ('SKU-CAP', 'revive_relist', 'observing', 'P2', 1, ?, ?, ?, 1)
            """,
            ("2026-05-01T00:00:00+00:00", past_date, metrics_before)
        )
        conn.commit()
    
    _seed_snapshot(db, "SKU-CAP", impressions=60, views=6, transactions=0)
    
    res = lifecycle.evaluate_observations(db_path=db)
    assert res["evaluated"] == 1
    
    row = _actions(db)[0]
    assert row["status"] == "fallback_delist_pending"
    assert len(enqueued) == 0

def test_detect_candidates_skips_unverified_zero_traffic_for_end(tmp_path: Path):
    """Truncated top-200 miss must not become old_dead_link End."""
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-UNVERIFIED", published_at="2026-03-01T00:00:00")
    _seed_snapshot(db, "SKU-UNVERIFIED", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=lambda _lid: None,
    )

    assert result["created"] == 0
    assert result["skipped"].get("traffic_unverified_for_end") == 1
    assert _actions(db) == []


def test_detect_candidates_age_uses_as_of_date_not_stale_snapshot(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-AGE", published_at="2026-04-01T00:00:00")
    _seed_snapshot(db, "SKU-AGE", snapshot_date="2026-05-13", impressions=10, views=2)

    # Run day is later than snapshot day; age must follow as_of_date.
    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-06-01",
        traffic_lookup=lambda _lid: {
            "impressions": 10,
            "views": 2,
            "transactions": 0,
            "verified": True,
            "source": "test",
        },
    )
    assert result["created"] == 1
    metrics = json.loads(_actions(db)[0]["metrics_before_json"])
    assert metrics["age_days"] == 61
    assert metrics["as_of_date"] == "2026-06-01"


# Main 2026-10-23 replay rows. Ages are run-day 2026-10-24 minus published_at.
_REAL_SKU_ROWS = (
    ("W2660P369122", "366352780364", "2026-04-18T00:00:00", 189),
    ("W2787P342133", "366484499792", "2026-06-18T00:00:00", 128),
    ("W5709P565684", "366689590282", "2026-09-24T00:00:00", 30),
)


def _seed_real_skus(db: Path) -> None:
    _create_source_tables(db)
    for sku, listing_id, published_at, _age in _REAL_SKU_ROWS:
        _seed_product(db, sku, listing_id=listing_id, published_at=published_at)
        _seed_snapshot(
            db,
            sku,
            snapshot_date="2026-10-23",
            listing_id=listing_id,
            impressions=0,
            views=0,
            transactions=0,
            sold_qty=0,
        )


def _lifecycle_counts(db: Path) -> tuple[int, int]:
    with sqlite3.connect(str(db)) as conn:
        actions = conn.execute(
            "SELECT COUNT(*) FROM cro_listing_lifecycle_actions"
        ).fetchone()[0]
        events = conn.execute(
            "SELECT COUNT(*) FROM cro_listing_lifecycle_action_events"
        ).fetchone()[0]
    return int(actions), int(events)


def _metrics(row: dict) -> dict:
    return json.loads(row["metrics_before_json"])


def test_detect_candidates_does_not_read_runtime_performance_cache(tmp_path, monkeypatch):
    def _explode(max_age_hours=72):
        raise AssertionError("runtime performance cache must not be read")

    monkeypatch.setattr("src.services.ebay_performance.load_performance_cache", _explode)
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-HERMETIC")
    _seed_snapshot(db, "SKU-HERMETIC", impressions=9, views=1)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["created"] == 1
    assert result["traffic_meta"] == _TRUNCATED_TRAFFIC_META


def test_real_skus_truncated_top200_skips_unverified_end(tmp_path, monkeypatch):
    """W2660 and W2787 are not in the truncated report; W5709 is only 30 days old."""
    monkeypatch.setattr(
        lifecycle, "_load_performance_traffic_index", _truncated_performance_index
    )
    db = tmp_path / "cro.db"
    _seed_real_skus(db)

    result = detect_candidates(
        snapshot_date="2026-10-23",
        db_path=db,
        as_of_date="2026-10-24",
        traffic_lookup=None,
    )

    assert result["as_of_date"] == "2026-10-24"
    assert result["snapshot_date"] == "2026-10-23"
    assert result["scanned"] == 3
    assert result["created"] == 1
    assert result["skipped"].get("traffic_unverified_for_end") == 2
    assert result["traffic_meta"]["is_truncated"] is True
    assert result["traffic_meta"]["record_count"] == 200
    rows = _actions(db)
    assert [row["sku"] for row in rows] == ["W5709P565684"]
    assert rows[0]["action_type"] == "revive_relist"
    assert rows[0]["priority"] == "P3"
    metrics = _metrics(rows[0])
    assert metrics["age_days"] == 30
    assert metrics["traffic_verified"] is False
    assert metrics["impressions"] == 0
    assert metrics["traffic_source"] == "unverified_zero_not_in_top200"


def test_real_skus_verified_zero_lookup_ends_only_the_old_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle, "_load_performance_traffic_index", _truncated_performance_index
    )
    db = tmp_path / "cro.db"
    _seed_real_skus(db)

    result = detect_candidates(
        snapshot_date="2026-10-23",
        db_path=db,
        as_of_date="2026-10-24",
        traffic_lookup=lambda _lid: {
            "impressions": 0,
            "views": 0,
            "transactions": 0,
            "verified": True,
            "source": "test",
        },
    )

    assert result["created"] == 3
    assert result["skipped"].get("traffic_unverified_for_end", 0) == 0
    by_sku = {row["sku"]: row for row in _actions(db)}
    for sku, _listing_id, _published_at, age in _REAL_SKU_ROWS[:2]:
        assert by_sku[sku]["action_type"] == "final_delist"
        assert by_sku[sku]["priority"] == "P3"
        metrics = _metrics(by_sku[sku])
        assert metrics["age_days"] == age
        assert metrics["traffic_verified"] is True
        assert metrics["impressions"] == 0
        assert metrics["views"] == 0
    young = by_sku["W5709P565684"]
    assert young["action_type"] == "revive_relist"
    assert young["priority"] == "P3"
    assert _metrics(young)["age_days"] == 30


def test_real_sku_positive_lookup_revives_instead_of_end(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle, "_load_performance_traffic_index", _truncated_performance_index
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    sku, listing_id, published_at, age = _REAL_SKU_ROWS[0]
    _seed_product(db, sku, listing_id=listing_id, published_at=published_at)
    _seed_snapshot(
        db,
        sku,
        snapshot_date="2026-10-23",
        listing_id=listing_id,
        impressions=0,
        views=0,
    )

    result = detect_candidates(
        snapshot_date="2026-10-23",
        db_path=db,
        as_of_date="2026-10-24",
        traffic_lookup=lambda _lid: {
            "impressions": 18,
            "views": 3,
            "transactions": 0,
            "verified": True,
            "source": "per_listing",
        },
    )

    assert result["created"] == 1
    assert result["skipped"].get("traffic_unverified_for_end", 0) == 0
    row = _actions(db)[0]
    assert row["sku"] == sku
    assert row["action_type"] == "revive_relist"
    assert row["priority"] == "P2"
    metrics = _metrics(row)
    assert metrics["age_days"] == age
    assert metrics["impressions"] == 18
    assert metrics["views"] == 3
    assert metrics["traffic_verified"] is True
    assert metrics["traffic_source"] == "per_listing"


@pytest.mark.parametrize("payload", [None, {}, {"traffic": {}}])
def test_missing_or_empty_cache_does_not_verify_zero_for_end(tmp_path, monkeypatch, payload):
    _use_real_performance_cache(monkeypatch, payload)
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-STALE", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-STALE", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=None,
    )

    assert result["created"] == 0
    assert result["skipped"].get("traffic_unverified_for_end") == 1
    assert _actions(db) == []
    meta = result["traffic_meta"]
    assert not (meta.get("api_ok") and not meta.get("is_truncated"))
    if payload is None or payload == {}:
        assert meta == {
            "api_ok": False,
            "is_truncated": True,
            "record_count": 0,
            "limit": 200,
            "reason": "cache_missing_or_expired",
        }


def test_missing_cache_still_ends_when_lookup_verifies_zero(tmp_path, monkeypatch):
    _use_real_performance_cache(monkeypatch, None)
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-STALE", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-STALE", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=_verified_zero_traffic,
    )

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "final_delist"
    assert _metrics(row)["age_days"] == 60
    assert _metrics(row)["traffic_verified"] is True


def test_complete_report_absence_is_verified_zero_final_delist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_load_performance_traffic_index",
        lambda: (
            {},
            {"api_ok": True, "is_truncated": False, "record_count": 10, "limit": 200},
        ),
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-COMPLETE", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-COMPLETE", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=None,
    )

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "final_delist"
    metrics = _metrics(row)
    assert metrics["traffic_verified"] is True
    assert metrics["traffic_source"] == "traffic_report_complete_absent"
    assert metrics["impressions"] == 0
    assert metrics["views"] == 0
    assert metrics["age_days"] == 60


def test_cache_hit_wins_over_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_load_performance_traffic_index",
        lambda: (
            {"L-HIT": {"impressions": 3, "views": 1, "transactions": 0}},
            dict(_TRUNCATED_TRAFFIC_META),
        ),
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-HIT", listing_id="L-HIT", published_at="2026-03-01T00:00:00")
    _seed_snapshot(db, "SKU-HIT", listing_id="L-HIT", impressions=99, views=40)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=lambda _lid: {
            "impressions": 0,
            "views": 0,
            "transactions": 0,
            "verified": True,
            "source": "should-not-win",
        },
    )

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "revive_relist"
    assert row["priority"] == "P2"
    metrics = _metrics(row)
    assert metrics["impressions"] == 3
    assert metrics["views"] == 1
    assert metrics["traffic_verified"] is True
    assert metrics["traffic_source"] == "performance_cache"


def test_cache_zero_wins_over_positive_snapshot_and_can_end(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_load_performance_traffic_index",
        lambda: (
            {"L-ZERO": {"impressions": 0, "views": 0, "transactions": 0}},
            dict(_TRUNCATED_TRAFFIC_META),
        ),
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-ZERO-CACHE", listing_id="L-ZERO", published_at="2026-03-01T00:00:00")
    _seed_snapshot(db, "SKU-ZERO-CACHE", listing_id="L-ZERO", impressions=50, views=5)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "final_delist"
    metrics = _metrics(row)
    assert metrics["impressions"] == 0
    assert metrics["views"] == 0
    assert metrics["traffic_source"] == "performance_cache"
    assert metrics["traffic_verified"] is True
    assert metrics["age_days"] == 73


def test_positive_snapshot_is_verified_when_cache_is_truncated(tmp_path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-POS", published_at="2026-03-01T00:00:00")
    _seed_snapshot(db, "SKU-POS", impressions=6, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=None,
    )

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "revive_relist"
    assert row["priority"] == "P2"
    metrics = _metrics(row)
    assert metrics["impressions"] == 6
    assert metrics["views"] == 0
    assert metrics["traffic_verified"] is True
    assert metrics["traffic_source"] == "cro_snapshot_positive"
    assert metrics["age_days"] == 73


def test_verified_transactions_skip_as_sales_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_load_performance_traffic_index",
        lambda: (
            {"CACHE-L": {"impressions": 0, "views": 0, "transactions": 4}},
            dict(_TRUNCATED_TRAFFIC_META),
        ),
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-CACHE", listing_id="CACHE-L")
    _seed_snapshot(db, "SKU-CACHE", listing_id="CACHE-L", impressions=0, views=0)
    _seed_product(db, "SKU-LIVE", listing_id="LIVE-L")
    _seed_snapshot(db, "SKU-LIVE", listing_id="LIVE-L", impressions=0, views=0)
    _seed_product(db, "SKU-OK", listing_id="OK-L")
    _seed_snapshot(db, "SKU-OK", listing_id="OK-L", impressions=5, views=1)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=lambda lid: {
            "impressions": 0,
            "views": 0,
            "transactions": 2,
            "verified": True,
            "source": "live",
        }
        if lid == "LIVE-L"
        else None,
    )

    assert result["created"] == 1
    assert result["skipped"].get("sales_present") == 2
    assert [row["sku"] for row in _actions(db)] == ["SKU-OK"]


def test_ended_and_delisted_local_status_skipped(tmp_path, monkeypatch):
    """ENDED/DELISTED rows are skipped before End or revive.

    The candidate query still selects only PUBLISHED, so this test appends the
    terminal rows after that query. The loop itself records the skip.
    """
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-ENDED", status="ENDED", listing_id="END-1", published_at="2026-01-01T00:00:00")
    _seed_product(db, "SKU-DELISTED", status="DELISTED", listing_id="DEL-1", published_at="2026-01-02T00:00:00")
    _seed_product(db, "SKU-LIVE", listing_id="LIVE-1")
    _seed_snapshot(db, "SKU-ENDED", listing_id="END-1", impressions=0, views=0)
    _seed_snapshot(db, "SKU-DELISTED", listing_id="DEL-1", impressions=0, views=0)
    _seed_snapshot(db, "SKU-LIVE", listing_id="LIVE-1", impressions=8, views=1)

    real_load = lifecycle._load_candidate_rows

    def _load_with_terminal_rows(conn, snapshot_date):
        rows = list(real_load(conn, snapshot_date))
        seen = {str(row["sku"]) for row in rows}
        extra = conn.execute(
            """
            SELECT p.*,
                   s.snapshot_date AS metrics_snapshot_date,
                   s.listing_id AS snapshot_listing_id,
                   COALESCE(s.impressions, 0) AS impressions,
                   COALESCE(s.views, 0) AS views,
                   COALESCE(s.transactions, 0) AS transactions,
                   COALESCE(s.sold_qty, 0) AS sold_qty
            FROM collected_products p
            LEFT JOIN cro_snapshots s
              ON s.sku = p.sku AND s.snapshot_date = ?
            WHERE UPPER(COALESCE(p.status, '')) IN ('ENDED', 'DELISTED')
            """,
            (snapshot_date,),
        )
        for row in extra:
            item = dict(row)
            if str(item["sku"]) not in seen:
                rows.append(item)
        return rows

    monkeypatch.setattr(lifecycle, "_load_candidate_rows", _load_with_terminal_rows)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["scanned"] == 3
    assert result["created"] == 1
    assert result["skipped"].get("already_ended_or_delisted") == 2
    assert [row["sku"] for row in _actions(db)] == ["SKU-LIVE"]


def test_persist_false_returns_candidates_without_inserts(tmp_path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-DRY")
    _seed_snapshot(db, "SKU-DRY", impressions=20, views=2)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        persist=False,
    )

    assert result["persist"] is False
    assert result["created"] == 1
    assert result["as_of_date"] == "2026-05-13"
    candidate = result["candidates"][0]
    assert candidate["id"] is None
    assert candidate["sku"] == "SKU-DRY"
    assert candidate["action_type"] == "revive_relist"
    assert _lifecycle_counts(db) == (0, 0)


def test_as_of_date_accepts_string_date_and_today(tmp_path, monkeypatch):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-ASOF", published_at="2026-04-01T00:00:00")
    _seed_snapshot(db, "SKU-ASOF", impressions=4, views=1)

    by_string = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        persist=False,
    )
    assert by_string["as_of_date"] == "2026-05-13"
    assert by_string["candidates"][0]["metrics"]["age_days"] == 42

    by_date = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date=date(2026, 5, 13),
        persist=False,
    )
    assert by_date["as_of_date"] == "2026-05-13"
    assert by_date["candidates"][0]["metrics"]["age_days"] == 42

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return date(2026, 6, 1)

    monkeypatch.setattr(lifecycle, "date", _FrozenDate)
    by_today = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date=None,
        persist=False,
    )
    assert by_today["as_of_date"] == "2026-06-01"
    assert by_today["candidates"][0]["metrics"]["age_days"] == 61


def test_as_of_date_invalid_string_raises(tmp_path):
    db = tmp_path / "cro.db"
    with pytest.raises(ValueError, match="Invalid as_of_date"):
        detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="not-a-date")


def test_default_live_traffic_lookup_is_off(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        lifecycle,
        "_lookup_listing_traffic_live",
        lambda listing_id: calls.append(listing_id),
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-NOLIVE", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-NOLIVE", impressions=0, views=0)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert calls == []
    assert result["created"] == 0
    assert result["skipped"].get("traffic_unverified_for_end") == 1


def test_historical_truncation_inferred_only_for_non_empty_traffic(tmp_path, monkeypatch):
    full = {
        str(i): {"impressions": 1, "views": 0, "transactions": 0}
        for i in range(200)
    }
    _use_real_performance_cache(monkeypatch, {"traffic": full})
    _index, meta = lifecycle._load_performance_traffic_index()
    assert meta["api_ok"] is True
    assert meta["is_truncated"] is True
    assert meta["record_count"] == 200
    assert meta["limit"] == 200

    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-ABSENT", listing_id="not-in-cache", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-ABSENT", listing_id="not-in-cache", impressions=0, views=0)
    absent = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=None,
    )
    assert absent["created"] == 0
    assert absent["skipped"].get("traffic_unverified_for_end") == 1

    _use_real_performance_cache(
        monkeypatch,
        {"traffic": {"999": {"impressions": 1, "views": 0, "transactions": 0}}},
    )
    _index, short_meta = lifecycle._load_performance_traffic_index()
    assert short_meta["api_ok"] is True
    assert short_meta["is_truncated"] is False
    assert short_meta["record_count"] == 1

    db_short = tmp_path / "cro-short.db"
    _create_source_tables(db_short)
    _seed_product(db_short, "SKU-ABSENT", listing_id="not-in-cache", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db_short, "SKU-ABSENT", listing_id="not-in-cache", impressions=0, views=0)
    complete = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db_short,
        as_of_date="2026-05-13",
        traffic_lookup=None,
    )
    assert complete["created"] == 1
    assert _actions(db_short)[0]["action_type"] == "final_delist"
    assert _metrics(_actions(db_short)[0])["traffic_source"] == "traffic_report_complete_absent"

    _use_real_performance_cache(monkeypatch, {"traffic": {}})
    empty_index, empty_meta = lifecycle._load_performance_traffic_index()
    assert empty_index == {}
    assert empty_meta == {}

    _use_real_performance_cache(
        monkeypatch,
        {
            "traffic": {"1": {"impressions": 1, "views": 0, "transactions": 0}},
            "traffic_meta": {
                "api_ok": True,
                "is_truncated": True,
                "record_count": 1,
                "limit": 200,
            },
        },
    )
    _index, kept = lifecycle._load_performance_traffic_index()
    assert kept["is_truncated"] is True


@pytest.mark.parametrize(
    "bad_entry",
    ["not-a-dict", [1, 2], None, 0, {"views": 0}, {"impressions": 0}],
    ids=[
        "string",
        "list",
        "none",
        "number",
        "dict_without_impressions",
        "dict_without_views",
    ],
)
def test_malformed_cache_entry_is_not_a_verified_zero(tmp_path, monkeypatch, bad_entry):
    """A broken per-listing row is unverified, even on a complete report."""
    listing_id = "366352780364"
    _use_real_performance_cache(
        monkeypatch,
        {
            "traffic": {listing_id: bad_entry},
            "traffic_meta": {
                "api_ok": True,
                "is_truncated": False,
                "record_count": 1,
                "limit": 200,
            },
        },
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-BAD", listing_id=listing_id, published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-BAD", listing_id=listing_id, impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=None,
    )

    assert result["created"] == 0
    assert result["skipped"].get("traffic_unverified_for_end") == 1
    assert _actions(db) == []
    assert all(row.get("action_type") != "final_delist" for row in result["candidates"])


def test_well_formed_zero_cache_hit_ends_on_complete_report(tmp_path, monkeypatch):
    _use_real_performance_cache(
        monkeypatch,
        {
            "traffic": {"L-OK": {"impressions": 0, "views": 0}},
            "traffic_meta": {
                "api_ok": True,
                "is_truncated": False,
                "record_count": 1,
                "limit": 200,
            },
        },
    )
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-OK", listing_id="L-OK", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-OK", listing_id="L-OK", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=None,
    )

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "final_delist"
    metrics = _metrics(row)
    assert metrics["age_days"] == 60
    assert metrics["impressions"] == 0
    assert metrics["views"] == 0
    assert metrics["traffic_verified"] is True
    assert metrics["traffic_source"] == "performance_cache"


def test_edge_lookup_verified_false_does_not_end_or_revive(tmp_path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-UNTRUSTED", published_at="2026-03-14T00:00:00")
    _seed_snapshot(db, "SKU-UNTRUSTED", impressions=0, views=0)

    result = detect_candidates(
        snapshot_date="2026-05-13",
        db_path=db,
        as_of_date="2026-05-13",
        traffic_lookup=lambda _lid: {
            "impressions": 80,
            "views": 9,
            "transactions": 0,
            "verified": False,
            "source": "untrusted",
        },
    )

    assert result["created"] == 0
    assert result["skipped"].get("traffic_unverified_for_end") == 1
    assert _actions(db) == []


def test_edge_whitespace_listing_id_is_missing_and_fetch_skips_request(tmp_path, monkeypatch):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-BLANK", listing_id="   ", published_at="2026-03-01T00:00:00")
    _seed_snapshot(db, "SKU-BLANK", listing_id="   ", impressions=0, views=0)
    _seed_product(db, "SKU-PADDED", listing_id="  L-PAD  ")
    _seed_snapshot(db, "SKU-PADDED", listing_id="L-PAD", impressions=0, views=0)
    monkeypatch.setattr(
        lifecycle,
        "_load_performance_traffic_index",
        lambda: (
            {"L-PAD": {"impressions": 7, "views": 2, "transactions": 0}},
            dict(_TRUNCATED_TRAFFIC_META),
        ),
    )

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db, as_of_date="2026-05-13")

    assert result["skipped"].get("missing_listing_id") == 1
    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["sku"] == "SKU-PADDED"
    assert row["old_listing_id"] == "L-PAD"
    metrics = _metrics(row)
    assert metrics["impressions"] == 7
    assert metrics["traffic_source"] == "performance_cache"

    import src.services.ebay_performance as perf

    def _forbid_get(*_args, **_kwargs):
        raise AssertionError("requests.get should not be called")

    monkeypatch.setattr(perf.requests, "get", _forbid_get)
    service = perf.EbayPerformanceService.__new__(perf.EbayPerformanceService)
    service.base = "https://api.ebay.test"
    assert service.fetch_traffic_for_listing("   ") is None
    assert service.fetch_traffic_for_listing("") is None
    assert service.fetch_traffic_for_listing(None) is None


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _traffic_service():
    import src.services.ebay_performance as perf

    service = perf.EbayPerformanceService.__new__(perf.EbayPerformanceService)
    service.base = "https://api.ebay.test"
    service._headers = lambda: {"Authorization": "Bearer t"}
    return service


def test_fetch_traffic_for_listing_empty_records_are_verified_zeros(monkeypatch):
    import src.services.ebay_performance as perf

    captured = {}

    def _get(url, headers=None, params=None, timeout=None, verify=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(200, {"records": [], "header": {"metrics": []}})

    monkeypatch.setattr(perf.requests, "get", _get)
    service = _traffic_service()
    row = service.fetch_traffic_for_listing("366352780364")

    assert row == {
        "impressions": 0,
        "views": 0,
        "ctr": 0.0,
        "conversion_rate": 0.0,
        "transactions": 0,
    }
    assert captured["url"].endswith("/sell/analytics/v1/traffic_report")
    assert "listing_ids:{366352780364}" in captured["params"]["filter"]


def test_fetch_traffic_for_listing_maps_one_record(monkeypatch):
    import src.services.ebay_performance as perf

    captured = {}

    def _get(url, headers=None, params=None, timeout=None, verify=None):
        captured["params"] = params
        return _FakeResponse(
            200,
            {
                "header": {
                    "metrics": [
                        {"key": "LISTING_IMPRESSION_TOTAL"},
                        {"key": "LISTING_VIEWS_TOTAL"},
                        {"key": "CLICK_THROUGH_RATE"},
                        {"key": "SALES_CONVERSION_RATE"},
                        {"key": "TRANSACTION"},
                    ]
                },
                "records": [
                    {
                        "metricValues": [
                            {"value": 120, "applicable": True},
                            {"value": 15, "applicable": True},
                            {"value": 0.125, "applicable": True},
                            {"value": 0.04, "applicable": True},
                            {"value": 2, "applicable": True},
                        ]
                    }
                ],
            },
        )

    monkeypatch.setattr(perf.requests, "get", _get)
    row = _traffic_service().fetch_traffic_for_listing("366484499792")

    assert row == {
        "impressions": 120,
        "views": 15,
        "ctr": 0.125,
        "conversion_rate": 0.04,
        "transactions": 2,
    }
    assert "listing_ids:{366484499792}" in captured["params"]["filter"]


def test_fetch_traffic_for_listing_non_200_and_exception_return_none(monkeypatch):
    import src.services.ebay_performance as perf

    monkeypatch.setattr(
        perf.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(503, text="unavailable"),
    )
    assert _traffic_service().fetch_traffic_for_listing("366689590282") is None

    def _boom(*args, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(perf.requests, "get", _boom)
    assert _traffic_service().fetch_traffic_for_listing("366689590282") is None


def test_fetch_traffic_for_listing_empty_id_does_not_call_requests(monkeypatch):
    import src.services.ebay_performance as perf

    def _forbid_get(*_args, **_kwargs):
        raise AssertionError("requests.get should not be called")

    monkeypatch.setattr(perf.requests, "get", _forbid_get)
    service = _traffic_service()
    assert service.fetch_traffic_for_listing("") is None
    assert service.fetch_traffic_for_listing(None) is None

