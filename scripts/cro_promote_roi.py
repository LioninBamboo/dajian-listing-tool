"""S33 \u2014 promote ROI \u95ed\u73af.

\u5bf9\u6bcf\u4e2a\u8fd1 N \u5929\u6210\u529f promote \u7684 SKU:
  - before \u7a97\u53e3 (promote \u524d 7d) \u7684\u9500\u552e\u91cf vs after (promote \u540e 7d)
  - lift_units = after_units - before_units
  - \u9884\u4f30 ad_spend = bid_increment_pct% \u00d7 \u4ef7\u683c \u00d7 after_units
  - lift_revenue = lift_units \u00d7 \u4ef7\u683c
  - ROI = lift_revenue / ad_spend (\u8d8a\u5927\u8d8a\u597d, < 1.0 \u8868\u793a\u5e7f\u544a\u4e0d\u8d5a)

\u8f93\u51fa: rows + by_sku + by_action; ROI < ROI_ROLLBACK_THRESHOLD \u7684 SKU
\u4f1a\u88ab\u63a8\u8350\u8d70 promote_rollback (\u4e0b\u4e00\u8f6e slice \u5b9e\u73b0).

\u590d\u7528:
  - cro_action_queue (\u8bfb done promote rows)
  - cro_snapshots.sold_qty (\u4f5c\u4e3a\u9500\u552e\u4ee3\u7406)

\u63a5\u4e0b\u53bb\u53ef\u4ee5\u63a5 EbayAdService.fetch_campaign_report \u7528\u771f\u5b9e ad_spend.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'
DEFAULT_QUEUE = PROJECT_ROOT / 'logs' / 'cro_action_queue.jsonl'

WINDOW_DAYS = 7
ROI_ROLLBACK_THRESHOLD = 1.0  # ROI < 1 \u2192 \u5e7f\u544a\u8d54


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _iter_done_promotes(queue_path: Optional[Path] = None,
                        since_days: int = 60) -> List[Dict[str, Any]]:
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    if not qp.exists():
        return []
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=since_days)
    rows = []
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('action') != 'promote':
                continue
            if row.get('status') != 'done':
                continue
            dt = _parse_ts(row.get('done_at') or '')
            if dt is None or dt < cutoff:
                continue
            row['_done_dt'] = dt
            rows.append(row)
    return rows


def _sum_sold_qty(db_path: Path, sku: str,
                  start_date: str, end_date: str) -> Tuple[int, float]:
    """\u8fd4\u56de (\u603b sold_qty, \u4e2d\u4f4d ourPrice). \u6837\u672c\u4e0d\u8db3 \u2192 (0, 0.0)."""
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT sold_qty, ourPrice FROM cro_snapshots "
            "WHERE sku = ? AND snapshot_date >= ? AND snapshot_date <= ?",
            (sku, start_date, end_date),
        ).fetchall()
    if not rows:
        return (0, 0.0)
    total_qty = sum(int(r['sold_qty'] or 0) for r in rows)
    prices = [float(r['ourPrice'] or 0) for r in rows if r['ourPrice']]
    median_price = sorted(prices)[len(prices) // 2] if prices else 0.0
    return (total_qty, median_price)


def compute_promote_roi(window_days: int = WINDOW_DAYS,
                        since_days: int = 60,
                        queue_path: Optional[Path] = None,
                        db_path: Optional[Path] = None,
                        ) -> Dict[str, Any]:
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    dbp = Path(db_path) if db_path else DEFAULT_DB
    promotes = _iter_done_promotes(qp, since_days=since_days)

    rows: List[Dict[str, Any]] = []
    rollback_candidates: List[Dict[str, Any]] = []
    for p in promotes:
        sku = p.get('sku')
        done_dt: datetime = p['_done_dt']
        before_start = (done_dt - timedelta(days=window_days)).date().isoformat()
        before_end = (done_dt - timedelta(days=1)).date().isoformat()
        after_start = done_dt.date().isoformat()
        after_end = (done_dt + timedelta(days=window_days)).date().isoformat()

        before_qty, before_price = _sum_sold_qty(dbp, sku, before_start, before_end)
        after_qty, after_price = _sum_sold_qty(dbp, sku, after_start, after_end)
        if after_qty == 0 and before_qty == 0:
            continue
        price = after_price or before_price
        bid_inc_pct = float(p.get('detail', {}).get('suggested_bid_pct')
                            or p.get('increment') or 5.0) / 100.0
        # \u8c03\u7528\u8005\u5982\u679c\u63d0\u4f9b\u4e86\u771f\u5b9e ad_spend, \u4f18\u5148\u4f7f\u7528
        ad_spend_real = p.get('detail', {}).get('ad_spend_real')
        if ad_spend_real is not None:
            ad_spend = float(ad_spend_real)
        else:
            ad_spend = bid_inc_pct * price * after_qty
        lift_units = after_qty - before_qty
        lift_revenue = lift_units * price
        roi = (lift_revenue / ad_spend) if ad_spend > 0 else None
        row = {
            'sku': sku,
            'done_at': done_dt.isoformat(),
            'before_qty': before_qty,
            'after_qty': after_qty,
            'lift_units': lift_units,
            'price': round(price, 2),
            'ad_spend_est': round(ad_spend, 2),
            'lift_revenue': round(lift_revenue, 2),
            'roi': round(roi, 2) if roi is not None else None,
            'bid_inc_pct': round(bid_inc_pct * 100, 1),
        }
        rows.append(row)
        if roi is not None and roi < ROI_ROLLBACK_THRESHOLD:
            rollback_candidates.append(row)

    return {
        'window_days': window_days,
        'since_days': since_days,
        'evaluated': len(rows),
        'rollback_candidates': rollback_candidates,
        'rollback_threshold': ROI_ROLLBACK_THRESHOLD,
        'rows': rows,
    }


def render_roi_html(report: Dict[str, Any]) -> str:
    parts = ['<h3>\U0001F4B0 promote ROI</h3>']
    parts.append(
        f"<p>\u8bc4\u4f30 <b>{report['evaluated']}</b> SKU \u00b7 "
        f"\u9700\u56de\u9000 <b>{len(report['rollback_candidates'])}</b> "
        f"(ROI &lt; {report['rollback_threshold']})</p>"
    )
    parts.append('<table border=1 cellspacing=0 cellpadding=4>'
                 '<tr><th>sku</th><th>before</th><th>after</th>'
                 '<th>lift</th><th>price</th><th>ad spend</th>'
                 '<th>revenue</th><th>ROI</th></tr>')
    for r in sorted(report['rows'], key=lambda x: (x['roi'] or 0)):
        parts.append(
            f"<tr><td>{r['sku']}</td><td>{r['before_qty']}</td>"
            f"<td>{r['after_qty']}</td><td>{r['lift_units']}</td>"
            f"<td>{r['price']}</td><td>{r['ad_spend_est']}</td>"
            f"<td>{r['lift_revenue']}</td><td>{r['roi']}</td></tr>"
        )
    parts.append('</table>')
    return ''.join(parts)


def main():  # pragma: no cover
    import argparse
    p = argparse.ArgumentParser(description='S33 promote ROI report')
    p.add_argument('--window-days', type=int, default=WINDOW_DAYS)
    p.add_argument('--since-days', type=int, default=60)
    p.add_argument('--out', help='write json report')
    args = p.parse_args()
    rep = compute_promote_roi(window_days=args.window_days,
                              since_days=args.since_days)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )


if __name__ == '__main__':  # pragma: no cover
    main()
