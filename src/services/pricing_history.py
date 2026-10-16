"""价格 / Bid 历史快照 (P9, 2026-05).

每天 daily_tasks 末尾调一次 record_daily_snapshot, 把全部 ACTIVE SKU 的
(date, sku, listing_id, live_price, total_cost, current_bid_pct,
 max_safe_ad_rate, ad_status, blacklisted) 一行落进 `pricing_history` 表.

UNIQUE(date, sku) — 当日重跑会替换 (REPLACE INTO).

仪表盘 query: get_history(sku, days=30) → pandas-ready list[dict].
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)
DEFAULT_DB = Path(__file__).resolve().parents[2] / 'ebay_collection.db'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pricing_history (
    date TEXT NOT NULL,
    sku TEXT NOT NULL,
    listing_id TEXT,
    live_price REAL,
    total_cost REAL,
    current_bid_pct REAL,
    max_safe_ad_rate REAL,
    ad_status TEXT,
    blacklisted INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (date, sku)
)
"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_pricing_history_sku_date ON pricing_history(sku, date)"


def ensure_schema(db_path: Path = DEFAULT_DB) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_SCHEMA)
        conn.execute(_INDEX)
        conn.commit()
    finally:
        conn.close()


def record_daily_snapshot(rows: Iterable[Dict[str, Any]],
                           *, db_path: Path = DEFAULT_DB,
                           date: Optional[str] = None) -> int:
    """rows 字段: sku (必), listing_id, live_price, total_cost, current_bid_pct,
    max_safe_ad_rate, ad_status, blacklisted.

    Returns: 写入条数.
    """
    ensure_schema(db_path)
    date = date or datetime.now().strftime('%Y-%m-%d')
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        n = 0
        for r in rows:
            sku = r.get('sku')
            if not sku:
                continue
            cur.execute(
                """INSERT OR REPLACE INTO pricing_history
                   (date, sku, listing_id, live_price, total_cost,
                    current_bid_pct, max_safe_ad_rate, ad_status, blacklisted, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    date, sku, r.get('listing_id'),
                    _f(r.get('live_price')), _f(r.get('total_cost')),
                    _f(r.get('current_bid_pct')), _f(r.get('max_safe_ad_rate')),
                    r.get('ad_status'), int(bool(r.get('blacklisted'))), now,
                ),
            )
            n += 1
        conn.commit()
        logger.info(f"📸 pricing_history 写入 {n} 行 (date={date})")
        return n
    finally:
        conn.close()


def get_history(sku: str, days: int = 30,
                 db_path: Path = DEFAULT_DB) -> List[Dict[str, Any]]:
    ensure_schema(db_path)
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM pricing_history WHERE sku=? AND date>=? ORDER BY date ASC",
            (sku, since),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _f(v: Any) -> Optional[float]:
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(Decimal(str(v)))
        except Exception:
            return None
