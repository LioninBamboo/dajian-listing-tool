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


def _actions(db_path: Path) -> list[dict]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM cro_listing_lifecycle_actions ORDER BY id"
            )
        ]


def test_detect_candidates_creates_revive_action_for_30_day_no_sale(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-A")
    _seed_snapshot(db, "SKU-A", impressions=42, views=7)

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db)

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

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db)

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

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db)

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

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db)

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

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db)

    assert result["created"] == 1
    row = _actions(db)[0]
    assert row["action_type"] == "final_delist"
    assert row["sku"] == "SKU-DEAD-IMG"


def test_detect_candidates_prevents_duplicate_pending_action(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-DUP")
    _seed_snapshot(db, "SKU-DUP")

    first = detect_candidates(snapshot_date="2026-05-13", db_path=db)
    second = detect_candidates(snapshot_date="2026-05-13", db_path=db)

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

    result = detect_candidates(snapshot_date="2026-05-13", db_path=db)

    assert result["created"] == 0
    assert result["skipped"]["relist_attempt_limit"] == 1


def test_approve_actions_uses_allowed_transition(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-APPROVE")
    _seed_snapshot(db, "SKU-APPROVE")
    detect_candidates(snapshot_date="2026-05-13", db_path=db)
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
    detect_candidates(snapshot_date="2026-05-13", db_path=db)
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
    detect_candidates(snapshot_date="2026-05-13", db_path=db)
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


def test_execute_prechecked_relist_withdraws_and_publishes(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-RELIVE", listing_id="OLD-1")
    _seed_snapshot(db, "SKU-RELIVE", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db)
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


def test_execute_prechecked_relist_dry_run_failure_keeps_old_listing(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-BLOCK", listing_id="OLD-2")
    _seed_snapshot(db, "SKU-BLOCK", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db)
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


def test_execute_prechecked_relist_skips_when_sales_arrive_after_precheck(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-SALE-LATE", listing_id="OLD-3")
    _seed_snapshot(db, "SKU-SALE-LATE", impressions=55, views=5)
    detect_candidates(snapshot_date="2026-05-13", db_path=db)
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


def test_transition_action_rejects_invalid_status_jump(tmp_path: Path):
    db = tmp_path / "cro.db"
    _create_source_tables(db)
    _seed_product(db, "SKU-JUMP")
    _seed_snapshot(db, "SKU-JUMP")
    detect_candidates(snapshot_date="2026-05-13", db_path=db)
    action_id = _actions(db)[0]["id"]

    with pytest.raises(ValueError):
        transition_action(action_id, "ready_to_publish", db_path=db)

    assert _actions(db)[0]["status"] == "candidate"

from datetime import datetime, timedelta, timezone

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
    assert enqueued[0][0]["action"] == "promoted_listings"

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

