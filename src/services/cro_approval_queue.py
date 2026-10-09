"""S37 \u2014 \u7edf\u4e00\u5ba1\u6279\u961f\u5217.

\u805a\u5408\u9700\u4eba\u5de5\u51b3\u7b56\u7684\u4e09\u7c7b\u52a8\u4f5c:
  1. delist \u5f85\u70b9\u51fb (cro_delist_pending, S25)
  2. \u9608\u503c\u5f85\u63a8\u751f\u4ea7 (cro_thresholds_pending, S21)
  3. promote ROI<1.0 \u5f85\u56de\u9000 (\u8c03\u7528 cro_promote_roi.compute_promote_roi, S33)

\u7edf\u4e00\u8f93\u51fa\u7ed3\u6784: {kind, sku|category_id, summary, magic_link?, action_hint, ts}
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'


def _delist_pending(db_path: Path) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                "SELECT sku, token, expires_at FROM cro_delist_pending "
                "WHERE confirmed_at IS NULL ORDER BY expires_at"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    now = datetime.now().isoformat()
    for r in rows:
        if r['expires_at'] and r['expires_at'] < now:
            continue  # \u8fc7\u671f\u8df3\u8fc7
        out.append({
            'kind': 'delist',
            'sku': r['sku'],
            'summary': f"\u4e0b\u67b6\u4faf\u9009 (token \u5230\u671f {r['expires_at']})",
            'magic_link_token': r['token'],
            'action_hint': '\u5224\u65ad\u662f\u5426\u4e0b\u67b6 (\u4e0d\u53ef\u9006)',
            'ts': r['expires_at'],
        })
    return out


def _threshold_pending(db_path: Path) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                "SELECT category_id, ctr, cvr, str_pct, samples "
                "FROM cro_thresholds_pending"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    for r in rows:
        out.append({
            'kind': 'threshold_promote',
            'category_id': r['category_id'],
            'summary': f"CTR={r['ctr']} CVR={r['cvr']} STR={r['str_pct']} (n={r['samples']})",
            'action_hint': '\u5168\u91cf shadow \u540e\u63a8\u4ea7',
            'ts': None,
        })
    return out


def _roi_rollback(roi_compute=None) -> List[Dict[str, Any]]:
    if roi_compute is None:
        try:
            from scripts.cro_promote_roi import compute_promote_roi
            roi_compute = compute_promote_roi
        except Exception:
            return []
    try:
        rep = roi_compute()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for row in rep.get('rollback_candidates', []):
        out.append({
            'kind': 'promote_rollback',
            'sku': row.get('sku'),
            'summary': f"ROI={row.get('roi')} lift={row.get('lift_units')} "
                       f"spend={row.get('ad_spend_est')}",
            'action_hint': 'bid \u56de\u8c03\u5230 \u63a8\u5e7f\u524d\u6c34\u5e73',
            'ts': row.get('done_at'),
        })
    return out


def collect_pending(db_path: Optional[Path] = None,
                    include_delist: bool = True,
                    include_threshold: bool = True,
                    include_roi: bool = True,
                    roi_compute=None,
                    ) -> Dict[str, Any]:
    db = Path(db_path) if db_path else DEFAULT_DB
    items: List[Dict[str, Any]] = []
    if include_delist:
        items.extend(_delist_pending(db))
    if include_threshold:
        items.extend(_threshold_pending(db))
    if include_roi:
        items.extend(_roi_rollback(roi_compute=roi_compute))
    return {
        'collected_at': datetime.now().isoformat(timespec='seconds'),
        'total': len(items),
        'by_kind': {k: sum(1 for i in items if i['kind'] == k)
                    for k in ('delist', 'threshold_promote', 'promote_rollback')},
        'items': items,
    }


def render_digest_text(report: Dict[str, Any]) -> str:
    lines = [f"\u5f85\u5ba1\u6279\u52a8\u4f5c {report['total']} \u9879 ({report['collected_at']})"]
    bk = report['by_kind']
    lines.append(f"  \u4e0b\u67b6 {bk['delist']} \u00b7 \u9608\u503c {bk['threshold_promote']} "
                 f"\u00b7 ROI \u56de\u8c03 {bk['promote_rollback']}")
    for it in report['items']:
        ident = it.get('sku') or it.get('category_id') or '?'
        lines.append(f"  [{it['kind']}] {ident} \u2014 {it['summary']}")
    return '\n'.join(lines)
