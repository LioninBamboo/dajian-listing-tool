"""CRO 诊断快照持久化 — 用于做趋势对比 (今日 vs 昨日 / 7d).

存储: sqlite `cro_snapshots` 表 (按 sku + snapshot_date 唯一).
作用: daily_tasks 每日跑完 diagnose_batch 后落表; UI / 报告对比改善 vs 恶化.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_DB = Path(__file__).resolve().parents[2] / "ebay_collection.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cro_snapshots (
    snapshot_date TEXT NOT NULL,
    sku           TEXT NOT NULL,
    listing_id    TEXT,
    impressions   INTEGER DEFAULT 0,
    views         INTEGER DEFAULT 0,
    transactions  INTEGER DEFAULT 0,
    sold_qty      INTEGER DEFAULT 0,
    ctr           REAL DEFAULT 0,
    cvr           REAL DEFAULT 0,
    str_pct       REAL DEFAULT 0,
    funnel_stage  TEXT,
    cro_score     INTEGER DEFAULT 0,
    top_action    TEXT,
    actions_json  TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_date, sku)
);
CREATE INDEX IF NOT EXISTS idx_cro_snap_date ON cro_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_cro_snap_sku  ON cro_snapshots(sku);
"""


def _conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DEFAULT_DB
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    return c


def ensure_schema(db_path: Optional[Path] = None) -> None:
    with _conn(db_path) as c:
        c.executescript(_SCHEMA)


def save_snapshots(diagnoses: Iterable[Any], snapshot_date: Optional[str] = None,
                   db_path: Optional[Path] = None) -> int:
    """落盘一批诊断结果. 返回写入条数."""
    import json
    ensure_schema(db_path)
    sd = snapshot_date or date.today().isoformat()
    rows = []
    for d in diagnoses:
        top = d.actions[0].type if d.actions else None
        rows.append((
            sd, d.sku, d.listing_id,
            d.impressions, d.views, d.transactions, d.sold_qty,
            d.ctr, d.cvr, d.str_pct,
            d.funnel_stage, d.cro_score, top,
            json.dumps([a.to_dict() for a in d.actions], ensure_ascii=False),
        ))
    if not rows:
        return 0
    with _conn(db_path) as c:
        c.executemany(
            "INSERT OR REPLACE INTO cro_snapshots "
            "(snapshot_date, sku, listing_id, impressions, views, transactions, sold_qty,"
            " ctr, cvr, str_pct, funnel_stage, cro_score, top_action, actions_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        c.commit()
    return len(rows)


def load_snapshot(snapshot_date: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    ensure_schema(db_path)
    with _conn(db_path) as c:
        cur = c.execute(
            "SELECT * FROM cro_snapshots WHERE snapshot_date = ? ORDER BY cro_score ASC",
            (snapshot_date,),
        )
        return [dict(r) for r in cur.fetchall()]


def diff_snapshots(today: str, yesterday: str,
                   db_path: Optional[Path] = None) -> Dict[str, Any]:
    """对比两天: 改善 / 恶化 / 新增 P1 / 解除 P1 的 SKU 集合."""
    a = {r['sku']: r for r in load_snapshot(today, db_path)}
    b = {r['sku']: r for r in load_snapshot(yesterday, db_path)}
    improved, worsened, new_p1, cleared_p1 = [], [], [], []
    for sku, ra in a.items():
        rb = b.get(sku)
        if rb is None:
            if ra['top_action']:
                new_p1.append(sku)
            continue
        if ra['cro_score'] - rb['cro_score'] >= 5:
            improved.append(sku)
        elif rb['cro_score'] - ra['cro_score'] >= 5:
            worsened.append(sku)
        if rb['top_action'] and not ra['top_action']:
            cleared_p1.append(sku)
        if ra['top_action'] and not rb['top_action']:
            new_p1.append(sku)
    return {
        'today': today,
        'yesterday': yesterday,
        'improved': improved,
        'worsened': worsened,
        'new_p1': new_p1,
        'cleared_p1': cleared_p1,
        'today_total': len(a),
        'yesterday_total': len(b),
    }


def yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()
