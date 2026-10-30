"""S31 — A/B lift \u91cf\u5316.

\u5bf9\u6bcf\u4e2a (action, category) bucket:
  - treatment = \u5df2\u6267\u884c (status=done, cohort=treatment) \u7684 SKU\u5728 done_at \u00b1 window_days \u7684 cro_score \u53d8\u5316
  - control   = \u672a\u6267\u884c (cohort=control) \u7684 SKU \u4ece enqueued_at \u5230 +window_days \u7684 cro_score \u53d8\u5316
  - lift = mean(treatment_delta) - mean(control_delta)
  - 95% CI \u7528 bootstrap (n=1000)
\u4ec5 AB_ENABLED_ACTIONS = {image_refresh, promote} \u53c2\u4e0e (price_drop \u4e0d\u5165 A/B).
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

from src.services.cro_ab import AB_ENABLED_ACTIONS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'
DEFAULT_QUEUE = PROJECT_ROOT / 'logs' / 'cro_action_queue.jsonl'

BOOTSTRAP_N = 1000
SIG_THRESHOLD = 0.0  # CI \u4e0d\u8de8 0 \u8868\u793a\u663e\u8457


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _iter_ab_queue_rows(queue_path: Optional[Path] = None,
                        since_days: int = 60) -> List[Dict[str, Any]]:
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    if not qp.exists():
        return []
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=since_days)
    out = []
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('action') not in AB_ENABLED_ACTIONS:
                continue
            if row.get('cohort') not in {'treatment', 'control'}:
                continue
            anchor_ts = row.get('done_at') if row.get('cohort') == 'treatment' \
                else row.get('enqueued_at')
            dt = _parse_ts(anchor_ts or '')
            if dt is None or dt < cutoff:
                continue
            row['_anchor_dt'] = dt
            out.append(row)
    return out


def _snapshot_score(db_path: Path, sku: str, target_date: str,
                    direction: str = 'before') -> Optional[float]:
    """direction='before': latest snapshot \u2264 target. direction='after': \u2265 target."""
    op = '<=' if direction == 'before' else '>='
    order = 'DESC' if direction == 'before' else 'ASC'
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            f"SELECT cro_score FROM cro_snapshots "
            f"WHERE sku = ? AND snapshot_date {op} ? "
            f"ORDER BY snapshot_date {order} LIMIT 1",
            (sku, target_date),
        ).fetchone()
        if not row:
            return None
        return float(row['cro_score']) if row['cro_score'] is not None else None


def _bootstrap_ci(deltas: List[float], n: int = BOOTSTRAP_N,
                  seed: int = 42, alpha: float = 0.05) -> Tuple[float, float]:
    if len(deltas) < 2:
        return (float('nan'), float('nan'))
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        resample = [rng.choice(deltas) for _ in range(len(deltas))]
        samples.append(mean(resample))
    samples.sort()
    lo = samples[int(n * alpha / 2)]
    hi = samples[int(n * (1 - alpha / 2))]
    return (lo, hi)


def compute_ab_lift(window_days: int = 7,
                    since_days: int = 60,
                    queue_path: Optional[Path] = None,
                    db_path: Optional[Path] = None,
                    ) -> Dict[str, Any]:
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    dbp = Path(db_path) if db_path else DEFAULT_DB
    rows = _iter_ab_queue_rows(qp, since_days=since_days)

    # bucket by (action, cohort) → deltas
    buckets: Dict[Tuple[str, str], List[float]] = {}
    for r in rows:
        sku = r.get('sku')
        if not sku:
            continue
        anchor: datetime = r['_anchor_dt']
        before_d = anchor.date().isoformat()
        after_d = (anchor + timedelta(days=window_days)).date().isoformat()
        before = _snapshot_score(dbp, sku, before_d, 'before')
        after = _snapshot_score(dbp, sku, after_d, 'after')
        if before is None or after is None:
            continue
        delta = after - before
        key = (r['action'], r['cohort'])
        buckets.setdefault(key, []).append(delta)

    actions_seen = sorted({k[0] for k in buckets})
    by_action: Dict[str, Dict[str, Any]] = {}
    for at in actions_seen:
        t = buckets.get((at, 'treatment'), [])
        c = buckets.get((at, 'control'), [])
        if not t or not c:
            by_action[at] = {
                'treatment_n': len(t),
                'control_n': len(c),
                'treatment_mean': round(mean(t), 3) if t else None,
                'control_mean': round(mean(c), 3) if c else None,
                'lift': None,
                'ci_low': None,
                'ci_high': None,
                'significant': False,
            }
            continue
        t_mean = mean(t)
        c_mean = mean(c)
        # lift = treatment_mean - control_mean ; CI \u7528 treatment-only bootstrap \u53d6\u8fd1\u4f3c
        ci_low, ci_high = _bootstrap_ci([d - c_mean for d in t])
        significant = bool(
            ci_low == ci_low and ci_high == ci_high  # not NaN
            and (ci_low > SIG_THRESHOLD or ci_high < SIG_THRESHOLD)
        )
        by_action[at] = {
            'treatment_n': len(t),
            'control_n': len(c),
            'treatment_mean': round(t_mean, 3),
            'control_mean': round(c_mean, 3),
            'lift': round(t_mean - c_mean, 3),
            'ci_low': round(ci_low, 3),
            'ci_high': round(ci_high, 3),
            'significant': significant,
        }
    return {
        'window_days': window_days,
        'since_days': since_days,
        'by_action': by_action,
    }


def render_lift_html(report: Dict[str, Any]) -> str:
    parts = ['<h3>\U0001F9EA A/B Lift (treatment vs control)</h3>']
    parts.append('<table border=1 cellspacing=0 cellpadding=4>'
                 '<tr><th>action</th><th>T n</th><th>C n</th>'
                 '<th>T \u0394score</th><th>C \u0394score</th>'
                 '<th>lift</th><th>95% CI</th><th>significant</th></tr>')
    for at, s in report['by_action'].items():
        ci = f"[{s['ci_low']}, {s['ci_high']}]" if s.get('ci_low') is not None else '-'
        sig = '\u2705' if s.get('significant') else '\u274c'
        parts.append(
            f"<tr><td>{at}</td><td>{s['treatment_n']}</td><td>{s['control_n']}</td>"
            f"<td>{s.get('treatment_mean', '-')}</td><td>{s.get('control_mean', '-')}</td>"
            f"<td>{s.get('lift', '-')}</td><td>{ci}</td><td>{sig}</td></tr>"
        )
    parts.append('</table>')
    return ''.join(parts)


def main():  # pragma: no cover - CLI
    import argparse
    p = argparse.ArgumentParser(description='CRO A/B lift report (S31)')
    p.add_argument('--window-days', type=int, default=7)
    p.add_argument('--since-days', type=int, default=60)
    p.add_argument('--out', help='write json report to path')
    args = p.parse_args()
    rep = compute_ab_lift(window_days=args.window_days,
                          since_days=args.since_days)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )


if __name__ == '__main__':  # pragma: no cover
    main()
