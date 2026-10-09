"""Collection Database"""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from .collection_models import Base, CollectedProduct
from .database_safety import assert_runtime_not_in_maintenance, assert_single_link_database

# ?
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATABASE_PATH = os.path.join(BASE_DIR, 'ebay_collection.db')
MAINTENANCE_PATH = os.path.join(BASE_DIR, 'logs', '_maintenance.lock')
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# 
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},  # SQLite
    echo=False  # rueSQL
)

# Enable WAL mode for concurrent access
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    # Refuse the exact topology that caused the July 2026 split-WAL incident.
    assert_runtime_not_in_maintenance(MAINTENANCE_PATH)
    assert_single_link_database(DATABASE_PATH)
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


@event.listens_for(engine, "checkout")
def _check_sqlite_checkout(dbapi_conn, connection_record, connection_proxy):
    """Recheck safety even when SQLAlchemy reuses a pooled connection."""

    assert_runtime_not_in_maintenance(MAINTENANCE_PATH)
    assert_single_link_database(DATABASE_PATH)

# 
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 
def init_db():
    ""","""
    Base.metadata.create_all(bind=engine)
    print(f"[OK] Database initialized at: {DATABASE_URL}")

#  (FastAPI)
def get_db():
    """Get DB Session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
