import sqlite3

import pytest
from sqlalchemy import create_engine, event, text

from src.db import collection_db
from src.db.database_safety import DatabaseSafetyError


def test_pooled_connection_checkout_rejects_new_maintenance_lock(tmp_path, monkeypatch):
    database = tmp_path / "collection.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE collected_products (id INTEGER PRIMARY KEY)")
    maintenance = tmp_path / "_maintenance.lock"

    monkeypatch.setattr(collection_db, "DATABASE_PATH", str(database))
    monkeypatch.setattr(collection_db, "MAINTENANCE_PATH", str(maintenance))

    test_engine = create_engine(f"sqlite:///{database}")
    event.listen(test_engine, "checkout", collection_db._check_sqlite_checkout)
    with test_engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1

    maintenance.write_text("recovery in progress", encoding="utf-8")
    with pytest.raises(DatabaseSafetyError, match="maintenance lock"):
        with test_engine.connect():
            pass
    test_engine.dispose()
