"""ORM TolerantJSONList must load corrupt logs without dirtying or rewriting."""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.collection_models import Base, CollectedProduct, CorruptLogs
from tests.test_logs_codec import GARBAGE, TRAILING_SHORT, TRAILING_XML, VALID


def _make_engine(db_path):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    Base.metadata.create_all(bind=engine)
    return engine


def _insert_raw(db_path, rows):
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        for sku, logs in rows:
            conn.execute(
                """
                INSERT INTO collected_products (sku, title, price, status, logs)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sku, f"Title {sku}", 10.0, "COLLECTED", logs),
            )
        conn.commit()
    finally:
        conn.close()


def test_logs_column_is_text(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    engine = _make_engine(db_path)
    engine.dispose()
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        info = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(collected_products)")}
    finally:
        conn.close()
    assert str(info["logs"]).upper() == "TEXT"


def test_orm_all_does_not_raise_and_marks_corrupt(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    engine = _make_engine(db_path)
    _insert_raw(
        db_path,
        [
            ("OK1", VALID),
            ("BAD1", TRAILING_SHORT),
            ("BAD2", GARBAGE),
            ("XML1", TRAILING_XML),
        ],
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        products = session.query(CollectedProduct).all()
        by_sku = {product.sku: product for product in products}
        assert not session.dirty
        assert isinstance(by_sku["OK1"].logs, list)
        assert not isinstance(by_sku["OK1"].logs, CorruptLogs)
        assert isinstance(by_sku["BAD1"].logs, CorruptLogs)
        assert by_sku["BAD1"].logs.parse_status == "trailing_text"
        assert isinstance(by_sku["BAD2"].logs, CorruptLogs)
        assert by_sku["BAD2"].logs.parse_status == "garbage"
        assert isinstance(by_sku["XML1"].logs, CorruptLogs)
        assert "<?xml" in by_sku["XML1"].logs.trailing
        as_dict = by_sku["BAD1"].to_dict()
        assert as_dict["logs"][-1].startswith("[2026-10-08 11:26:48] LONG_OOS_END_LIVE")
    finally:
        session.close()
        engine.dispose()


def test_read_only_session_does_not_change_db_bytes(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    engine = _make_engine(db_path)
    _insert_raw(db_path, [("OK1", VALID), ("BAD1", TRAILING_SHORT)])
    engine.dispose()
    before = db_path.read_bytes()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        loaded = session.query(CollectedProduct).all()
        assert len(loaded) == 2
        assert not session.dirty
        assert not session.new
        assert not session.deleted
    finally:
        session.close()
        engine.dispose()

    assert db_path.read_bytes() == before


def test_corrupt_warning_omits_trailing_text(tmp_path, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="src.db.collection_models")
    db_path = tmp_path / "ebay_collection.db"
    engine = _make_engine(db_path)
    _insert_raw(db_path, [("XML1", TRAILING_XML)])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        session.query(CollectedProduct).all()
    finally:
        session.close()
        engine.dispose()
    assert "corrupt logs sku=XML1" in caplog.text
    assert "sku=?" not in caplog.text
    assert "status=trailing_text" in caplog.text
    assert "<?xml" not in caplog.text
    assert "EndFixedPriceItemResponse" not in caplog.text
    assert "Ack" not in caplog.text


def test_write_after_plus_stores_valid_json(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    engine = _make_engine(db_path)
    _insert_raw(db_path, [("BAD1", TRAILING_SHORT)])
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        product = session.query(CollectedProduct).one()
        from sqlalchemy.orm.attributes import flag_modified

        product.logs = list(product.logs) + ["x"]
        flag_modified(product, "logs")
        session.commit()
    finally:
        session.close()
        engine.dispose()

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        raw = conn.execute("SELECT logs FROM collected_products WHERE sku = ?", ("BAD1",)).fetchone()[0]
    finally:
        conn.close()
    loaded = json.loads(raw)
    assert loaded[-1] == "x"
    assert loaded[0] == "Listing: 366407614496, Offer: 108006576011, Cat: 38204"
    assert loaded[1].startswith("[2026-10-08 11:26:48] LONG_OOS_END_LIVE")
