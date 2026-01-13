"""Collection Database"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .collection_models import Base, CollectedProduct

# ?
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'ebay_collection.db')}"

# 
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite
    echo=False  # rueSQL
)

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
