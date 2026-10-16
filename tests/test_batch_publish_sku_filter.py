import sqlite3

import batch_publish


def test_get_ready_products_filters_by_sku_list_preserving_order(tmp_path, monkeypatch):
    db_path = tmp_path / "ebay_collection.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE collected_products (
                sku TEXT PRIMARY KEY,
                status TEXT,
                optimization TEXT,
                cost_breakdown TEXT,
                images TEXT,
                videos TEXT,
                specs TEXT,
                attributes TEXT,
                logs TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO collected_products (sku, status, optimization) VALUES (?, ?, ?)",
            [
                ("MI-2", "READY", "{}"),
                ("OTHER", "READY", "{}"),
                ("MI-1", "READY_TO_PUBLISH", "{}"),
                ("OLD", "PUBLISHED", "{}"),
            ],
        )

    monkeypatch.setattr(batch_publish, "PROJECT_ROOT", tmp_path)

    products = batch_publish.get_ready_products(sku_filters=["MI-1", "MI-2", "MISSING"])

    assert [p["sku"] for p in products] == ["MI-1", "MI-2"]
