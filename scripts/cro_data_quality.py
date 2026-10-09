"""S35 \u2014 \u591a\u6e90\u6570\u636e\u4ea4\u53c9\u6821\u9a8c.

\u5bf9\u540c\u4e00\u6279 SKU \u540c\u4e00\u7a97\u53e3 (\u8fd1 7d) \u7684 CTR/CVR/impression\uff0c
\u6bd4\u8f83 cro_snapshots (\u672c\u5730\u7f13\u5b58) vs eBay Marketing Insights (\u5b9e\u65f6\u67e5\u8be2)\u3002

\u4efb\u610f\u4e00\u4e2a\u6307\u6807 |a-b| / max(|a|,|b|) > DRIFT_THRESHOLD \u8868\u793a\u8be5 SKU \u6570\u636e\u8d28\u91cf\u53ef\u7591,
\u8f93\u51fa\u62a5\u544a, \u5e76\u8fd4\u56de blocked_skus \u5217\u8868\u4f9b\u4e0a\u6e38 cro_action_queue \u8fc7\u6ee4\u3002

\u4e0d\u8c03\u771f\u5b9e API \u603b\u53ef\u88ab\u6ce8\u5165\u7684 marketing_fetcher \u53c2\u6570 \u2192 \u53ef\u6d4b\u3002
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'
DEFAULT_OUT = PROJECT_ROOT / 'logs' / '_cro_data_quality.json'

DRIFT_THRESHOLD = 0.50  # 50% \u504f\u5dee
METRICS = ('impressions', 'ctr', 'cvr')


def _local_aggregate(db_path: Path, sku: str,
                     start: str, end: str) -> Dict[str, float]:
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT impressions, views, transactions FROM cro_snapshots "
            "WHERE sku=? AND snapshot_date>=? AND snapshot_date<=?",
            (sku, start, end),
        ).fetchall()
    if not rows:
        return {'impressions': 0.0, 'ctr': 0.0, 'cvr': 0.0}
    imp = sum(int(r['impressions'] or 0) for r in rows)
    views = sum(int(r['views'] or 0) for r in rows)
    txns = sum(int(r['transactions'] or 0) for r in rows)
    return {
        'impressions': float(imp),
        'ctr': (views / imp) if imp else 0.0,
        'cvr': (txns / views) if views else 0.0,
    }


def _drift(local: float, remote: float) -> float:
    denom = max(abs(local), abs(remote))
    if denom == 0:
        return 0.0
    return abs(local - remote) / denom


def cross_check(skus: List[str],
                marketing_fetcher: Callable[[str, str, str], Dict[str, float]],
                window_days: int = 7,
                threshold: float = DRIFT_THRESHOLD,
                db_path: Optional[Path] = None,
                ) -> Dict[str, Any]:
    """marketing_fetcher(sku, start, end) -> {impressions, ctr, cvr}."""
    db = Path(db_path) if db_path else DEFAULT_DB
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=window_days)).isoformat()
    rows: List[Dict[str, Any]] = []
    blocked: List[str] = []
    for sku in skus:
        local = _local_aggregate(db, sku, start, end)
        try:
            remote = marketing_fetcher(sku, start, end) or {}
        except Exception as e:
            rows.append({'sku': sku, 'error': str(e), 'blocked': True})
            blocked.append(sku)
            continue
        drifts = {m: _drift(local.get(m, 0.0), float(remote.get(m, 0.0)))
                  for m in METRICS}
        worst = max(drifts.values())
        is_blocked = worst > threshold
        rows.append({
            'sku': sku, 'local': local, 'remote': remote,
            'drift': drifts, 'worst_drift': round(worst, 3),
            'blocked': is_blocked,
        })
        if is_blocked:
            blocked.append(sku)
    return {
        'window_days': window_days,
        'threshold': threshold,
        'evaluated': len(rows),
        'blocked_skus': blocked,
        'rows': rows,
    }


def write_report(report: Dict[str, Any], out: Optional[Path] = None) -> Path:
    p = Path(out) if out else DEFAULT_OUT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                 encoding='utf-8')
    return p


def filter_data_quality_blocked(actions: List[Dict[str, Any]],
                                blocked_skus: List[str],
                                ) -> List[Dict[str, Any]]:
    if not blocked_skus:
        return actions
    s = set(blocked_skus)
    return [a for a in actions if a.get('sku') not in s]


def main():  # pragma: no cover
    import argparse
    p = argparse.ArgumentParser(description='S35 cross-check')
    p.add_argument('--out')
    args = p.parse_args()
    print('use as library; pass marketing_fetcher')
    if args.out:
        write_report({'evaluated': 0, 'rows': [], 'blocked_skus': []}, args.out)


if __name__ == '__main__':  # pragma: no cover
    main()
