"""S34 \u2014 \u5c0f\u65f6\u7ea7\u6f0f\u6597\u91c7\u6837.

\u4e0d\u4f9d\u8d56\u5b9e\u65f6 eBay API. \u601d\u8def:
  - \u8c03\u5ea6\u6bcf 2 \u5c0f\u65f6\u8c03\u4e00\u6b21 sample_funnel(): \u8bfb \u5f53\u65e5 cro_snapshots \u6700\u65b0\u72b6\u6001
  - \u6c47\u603b\u6210 funnel buckets \u8ba1\u6570 \u5199\u5165\u65b0\u8868 cro_funnel_hourly
  - \u770b\u677f\u8bfb\u8fd1 24h \u70b9, \u753b\u6298\u7ebf
\u8be5\u8868\u5728\u65b0\u73af\u5883\u4e0a\u4f1a\u81ea\u52a8\u521b\u5efa, \u4e0d\u7834\u574f\u73b0\u6709 cro_snapshots schema.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'

FUNNEL_STAGES = ('no_impression', 'low_ctr', 'low_cvr', 'low_str', 'healthy')


def ensure_schema(db_path: Optional[Path] = None) -> None:
    db = Path(db_path) if db_path else DEFAULT_DB
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cro_funnel_hourly (
                sampled_at TEXT PRIMARY KEY,
                no_impression INTEGER NOT NULL DEFAULT 0,
                low_ctr INTEGER NOT NULL DEFAULT 0,
                low_cvr INTEGER NOT NULL DEFAULT 0,
                low_str INTEGER NOT NULL DEFAULT 0,
                healthy INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.commit()


def sample_funnel(db_path: Optional[Path] = None,
                  snapshot_date: Optional[str] = None,
                  sampled_at: Optional[str] = None,
                  ) -> Dict[str, Any]:
    """\u8bfb\u4eca\u5929 cro_snapshots, \u6309 funnel_stage \u8ba1\u6570, \u5199 cro_funnel_hourly."""
    db = Path(db_path) if db_path else DEFAULT_DB
    ensure_schema(db)
    sd = snapshot_date or date.today().isoformat()
    ts = sampled_at or datetime.now().isoformat(timespec='minutes')
    counts = {s: 0 for s in FUNNEL_STAGES}
    total = 0
    with sqlite3.connect(str(db)) as c:
        try:
            for stage, n in c.execute(
                "SELECT funnel_stage, COUNT(*) FROM cro_snapshots "
                "WHERE snapshot_date = ? GROUP BY funnel_stage", (sd,)
            ):
                if stage in counts:
                    counts[stage] = int(n)
                total += int(n)
        except sqlite3.OperationalError:
            pass
        c.execute(
            "INSERT OR REPLACE INTO cro_funnel_hourly "
            "(sampled_at, no_impression, low_ctr, low_cvr, low_str, healthy, total) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, counts['no_impression'], counts['low_ctr'], counts['low_cvr'],
             counts['low_str'], counts['healthy'], total),
        )
        c.commit()
    return {'sampled_at': ts, 'snapshot_date': sd, 'total': total, **counts}


def load_recent_funnel(hours: int = 24,
                       db_path: Optional[Path] = None,
                       ) -> List[Dict[str, Any]]:
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec='minutes')
    out: List[Dict[str, Any]] = []
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        try:
            for r in c.execute(
                "SELECT * FROM cro_funnel_hourly WHERE sampled_at >= ? "
                "ORDER BY sampled_at ASC", (cutoff,)
            ):
                out.append(dict(r))
        except sqlite3.OperationalError:
            return []
    return out


def compute_deltas(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """\u8fd4\u56de\u6700\u8fd1\u4e00\u70b9 vs \u4e0a\u4e00\u70b9\u7684 funnel \u5404\u9636\u6bb5\u53d8\u5316."""
    if len(rows) < 2:
        return {'has_delta': False}
    prev, last = rows[-2], rows[-1]
    delta = {s: int(last.get(s, 0)) - int(prev.get(s, 0)) for s in FUNNEL_STAGES}
    return {
        'has_delta': True,
        'prev_at': prev['sampled_at'],
        'last_at': last['sampled_at'],
        'delta': delta,
        'last_total': last['total'],
    }


def main():  # pragma: no cover
    import argparse
    p = argparse.ArgumentParser(description='S34 hourly funnel sampler')
    p.add_argument('--show', action='store_true', help='\u53ea\u67e5\u8be2\u8fd1 24h \u4e0d\u91c7\u6837')
    args = p.parse_args()
    if not args.show:
        rep = sample_funnel()
        print(rep)
    rows = load_recent_funnel(24)
    print(f"24h \u91c7\u6837\u70b9 {len(rows)} \u4e2a")
    if rows:
        d = compute_deltas(rows)
        print(d)


if __name__ == '__main__':  # pragma: no cover
    main()
