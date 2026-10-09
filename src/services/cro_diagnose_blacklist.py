"""S29 \u2014 CRO \u8bca\u65ad\u9ed1\u540d\u5355.

\u8fd0\u8425\u4eba\u5de5\u68c0\u67e5\u540e\u8ba4\u4e3a\u8bef\u8bca/\u4e0d\u9002\u5408 CRO \u4f18\u5316\u7684 SKU (\u4f8b\u5982\u8054\u540d\u72ec\u5360\u3001\u5e93\u5b58\u9501\u5b9a\u3001\u5373\u5c06\u4e0b\u67b6),
\u5199\u5165 cro_diagnose_blacklist \u8868. cro_daily_runner \u5728 diagnose \u524d\u8fc7\u6ee4.

API:
  - add_to_blacklist(sku, reason)
  - remove_from_blacklist(sku)
  - is_blacklisted(sku)
  - load_blacklisted_skus() \u2192 set[str]
  - filter_blacklisted(products) \u2192 (kept, dropped)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'


def ensure_schema(db_path: Optional[Path] = None) -> None:
    db = Path(db_path) if db_path else DEFAULT_DB
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cro_diagnose_blacklist (
                sku TEXT PRIMARY KEY,
                reason TEXT,
                created_at TEXT NOT NULL
            )
        """)
        c.commit()


def add_to_blacklist(sku: str, reason: str = '',
                     db_path: Optional[Path] = None) -> None:
    db = Path(db_path) if db_path else DEFAULT_DB
    ensure_schema(db)
    with sqlite3.connect(str(db)) as c:
        c.execute(
            "INSERT OR REPLACE INTO cro_diagnose_blacklist (sku, reason, created_at) "
            "VALUES (?,?,?)",
            (str(sku), reason, datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def remove_from_blacklist(sku: str,
                          db_path: Optional[Path] = None) -> int:
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return 0
    with sqlite3.connect(str(db)) as c:
        cur = c.execute(
            "DELETE FROM cro_diagnose_blacklist WHERE sku = ?", (str(sku),)
        )
        c.commit()
        return cur.rowcount


def load_blacklisted_skus(db_path: Optional[Path] = None) -> Set[str]:
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return set()
    try:
        with sqlite3.connect(str(db)) as c:
            return {r[0] for r in c.execute(
                "SELECT sku FROM cro_diagnose_blacklist")}
    except sqlite3.OperationalError:
        return set()


def is_blacklisted(sku: str, db_path: Optional[Path] = None) -> bool:
    return str(sku) in load_blacklisted_skus(db_path)


def filter_blacklisted(products: List[Dict[str, Any]],
                       db_path: Optional[Path] = None,
                       ) -> Tuple[List[Dict[str, Any]], List[str]]:
    skus = load_blacklisted_skus(db_path)
    if not skus:
        return list(products), []
    kept, dropped = [], []
    for p in products:
        sku = str(p.get('sku') or '')
        if sku in skus:
            dropped.append(sku)
        else:
            kept.append(p)
    return kept, dropped


def list_blacklist(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return []
    try:
        with sqlite3.connect(str(db)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(
                "SELECT sku, reason, created_at FROM cro_diagnose_blacklist "
                "ORDER BY created_at DESC")]
    except sqlite3.OperationalError:
        return []
