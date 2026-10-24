"""CRO P1 效果验证 — 比较"建议执行日"和"7天后"的 funnel_stage / cro_score.

输入: cro_snapshots 表 + cro_action_queue.jsonl (status=done 的记录有 done_at)
输出: 每条 done 行 → before/after 对比, 标记 改善 / 持平 / 恶化.
用途: 周/月度回顾哪些 CRO 规则真的有效, 哪些应该调阈值或下线.

调度建议: 每月 1 日 09:30 之后 (daily_tasks 之后) 跑一次, 邮件给运营.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'
DEFAULT_QUEUE = PROJECT_ROOT / 'logs' / 'cro_action_queue.jsonl'


def _iter_done_actions(queue_path: Optional[Path] = None,
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
            if row.get('status') != 'done':
                continue
            done_at = row.get('done_at') or row.get('enqueued_at')
            if not done_at:
                continue
            try:
                dt = datetime.fromisoformat(done_at.replace('Z', '+00:00'))
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                if dt < cutoff:
                    continue
            except Exception:
                continue
            row['_done_dt'] = dt
            out.append(row)
    return out


def _snapshot(db_path: Path, sku: str, target_date: str) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            "SELECT * FROM cro_snapshots WHERE sku = ? AND snapshot_date <= ? "
            "ORDER BY snapshot_date DESC LIMIT 1",
            (sku, target_date),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _load_sku_to_category(db_path: Path) -> Dict[str, str]:
    """SKU → dajian_category 映射, 给 monthly_report 反哺阈值学习."""
    out: Dict[str, str] = {}
    if not db_path.exists():
        return out
    try:
        with sqlite3.connect(str(db_path)) as c:
            for sku, cat in c.execute(
                "SELECT sku, dajian_category FROM products "
                "WHERE dajian_category IS NOT NULL AND dajian_category != ''"
            ):
                out[sku] = cat
    except sqlite3.OperationalError:
        return {}
    return out


def evaluate_actions(window_days: int = 7,
                     since_days: int = 60,
                     queue_path: Optional[Path] = None,
                     db_path: Optional[Path] = None) -> Dict[str, Any]:
    """对每条 done action: 取 done 当天 vs window_days 天后的 snapshot, 算变化."""
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    dbp = Path(db_path) if db_path else DEFAULT_DB
    actions = _iter_done_actions(qp, since_days=since_days)
    sku_cat = _load_sku_to_category(dbp)

    rows: List[Dict[str, Any]] = []
    for a in actions:
        sku = a.get('sku')
        atype = a.get('action')
        done_dt: datetime = a['_done_dt']
        before = _snapshot(dbp, sku, done_dt.date().isoformat())
        after_target = (done_dt + timedelta(days=window_days)).date().isoformat()
        after = _snapshot(dbp, sku, after_target)
        if not before or not after:
            continue
        if after['snapshot_date'] <= before['snapshot_date']:
            continue
        delta_score = (after.get('cro_score') or 0) - (before.get('cro_score') or 0)
        if delta_score >= 5:
            verdict = 'improved'
        elif delta_score <= -5:
            verdict = 'worsened'
        else:
            verdict = 'flat'
        rows.append({
            'sku': sku,
            'action': atype,
            'category': sku_cat.get(sku) or '_unknown',
            'cohort': a.get('cohort') or 'na',
            'done_at': done_dt.isoformat(),
            'before_date': before['snapshot_date'],
            'after_date': after['snapshot_date'],
            'before_score': before.get('cro_score'),
            'after_score': after.get('cro_score'),
            'delta_score': delta_score,
            'before_stage': before.get('funnel_stage'),
            'after_stage': after.get('funnel_stage'),
            'verdict': verdict,
        })

    # 按 action 分组聚合
    by_action: Dict[str, Dict[str, int]] = {}
    for r in rows:
        d = by_action.setdefault(r['action'], {'total': 0, 'improved': 0, 'worsened': 0, 'flat': 0})
        d['total'] += 1
        d[r['verdict']] += 1

    return {
        'window_days': window_days,
        'total_evaluated': len(rows),
        'by_action': by_action,
        'rows': rows,
    }


def render_summary_text(report: Dict[str, Any]) -> str:
    lines = [f"CRO P1 效果验证 (window {report['window_days']}d, 共 {report['total_evaluated']} 条)"]
    for at, s in sorted(report['by_action'].items(),
                         key=lambda kv: -kv[1]['total']):
        if s['total'] == 0:
            rate = 0.0
        else:
            rate = s['improved'] / s['total'] * 100
        lines.append(
            f"  · {at:14s} 共{s['total']:3d}  改善{s['improved']:3d}({rate:.0f}%)  "
            f"持平{s['flat']:3d}  恶化{s['worsened']:3d}"
        )
    return '\n'.join(lines)


def monthly_report(window_days: int = 7,
                   since_days: int = 35,
                   db_path: Optional[Path] = None,
                   queue_path: Optional[Path] = None,
                   ) -> Dict[str, Any]:
    """S22 — 月度融合: effect_audit + 阈值反哺建议, 一次出 HTML."""
    from src.services.cro_thresholds import (
        load_thresholds, suggest_threshold_adjustments,
    )
    audit = evaluate_actions(window_days=window_days, since_days=since_days,
                             queue_path=queue_path, db_path=db_path)
    cur = load_thresholds(db_path)
    suggestions = suggest_threshold_adjustments(audit, cur)

    # by category aggregation
    by_cat: Dict[str, Dict[str, int]] = {}
    for r in audit['rows']:
        cat = r.get('category') or '_unknown'
        d = by_cat.setdefault(cat, {'total': 0, 'improved': 0,
                                     'worsened': 0, 'flat': 0})
        d['total'] += 1
        d[r['verdict']] = d.get(r['verdict'], 0) + 1

    rep = {
        'window_days': window_days,
        'since_days': since_days,
        'total_evaluated': audit['total_evaluated'],
        'by_action': audit['by_action'],
        'by_category': by_cat,
        'current_thresholds_count': len(cur),
        'suggestions': suggestions,
    }
    rep['html'] = _render_monthly_html(rep)
    return rep


def _render_monthly_html(rep: Dict[str, Any]) -> str:
    parts = ['<h2>📊 CRO 月报</h2>']
    parts.append(
        f"<p>窗口 <b>{rep['window_days']}</b> 天 · 回看 <b>{rep['since_days']}</b> 天 · "
        f"评估 <b>{rep['total_evaluated']}</b> 条 · "
        f"已学品类阈值 <b>{rep['current_thresholds_count']}</b> · "
        f"调整建议 <b>{len(rep['suggestions'])}</b> 个</p>"
    )
    parts.append('<h3>按动作</h3><table border=1 cellspacing=0 cellpadding=4>'
                 '<tr><th>action</th><th>total</th><th>改善</th>'
                 '<th>持平</th><th>恶化</th><th>改善率</th></tr>')
    for at, s in sorted(rep['by_action'].items(), key=lambda kv: -kv[1]['total']):
        rate = (s['improved'] / s['total'] * 100) if s['total'] else 0
        parts.append(
            f"<tr><td>{at}</td><td>{s['total']}</td><td>{s['improved']}</td>"
            f"<td>{s.get('flat', 0)}</td><td>{s['worsened']}</td>"
            f"<td>{rate:.0f}%</td></tr>"
        )
    parts.append('</table>')

    if rep['suggestions']:
        parts.append('<h3>阈值调整建议</h3><table border=1 cellspacing=0 cellpadding=4>'
                     '<tr><th>category</th><th>action</th><th>metric</th>'
                     '<th>current</th><th>suggested</th><th>direction</th>'
                     '<th>improved_rate</th><th>samples</th></tr>')
        for s in rep['suggestions']:
            parts.append(
                f"<tr><td>{s['category_id']}</td><td>{s['action']}</td>"
                f"<td>{s['metric']}</td><td>{s['current']}</td>"
                f"<td>{s['suggested']}</td><td>{s['direction']}</td>"
                f"<td>{s['improved_rate']*100:.0f}%</td><td>{s['samples']}</td></tr>"
            )
        parts.append('</table>')
    else:
        parts.append('<p>本月没有满足样本条件的阈值调整建议。</p>')

    if rep['by_category']:
        parts.append('<h3>按品类</h3><table border=1 cellspacing=0 cellpadding=4>'
                     '<tr><th>category</th><th>total</th><th>改善</th>'
                     '<th>持平</th><th>恶化</th></tr>')
        for cat, s in sorted(rep['by_category'].items(),
                              key=lambda kv: -kv[1]['total']):
            parts.append(
                f"<tr><td>{cat}</td><td>{s['total']}</td><td>{s['improved']}</td>"
                f"<td>{s.get('flat', 0)}</td><td>{s['worsened']}</td></tr>"
            )
        parts.append('</table>')
    return ''.join(parts)


def main():
    import argparse
    p = argparse.ArgumentParser(description='CRO P1 effect validation')
    p.add_argument('--window-days', type=int, default=7)
    p.add_argument('--since-days', type=int, default=60)
    p.add_argument('--out', help='write json report to path')
    p.add_argument('--monthly', action='store_true',
                   help='S22 月报: 融合 effect_audit + 阈值调整建议, 输出 HTML')
    p.add_argument('--email', action='store_true',
                   help='--monthly 配合: 邮件发送 HTML 月报')
    args = p.parse_args()

    if args.monthly:
        rep = monthly_report(window_days=args.window_days,
                             since_days=max(args.since_days, 35))
        print(f"评估 {rep['total_evaluated']} 条 / 调整建议 {len(rep['suggestions'])} 个")
        if args.out:
            Path(args.out).write_text(rep['html'], encoding='utf-8')
            print(f"HTML saved: {args.out}")
        if args.email:
            try:
                from src.services.email_notifier import send_email
                send_email(
                    f"📊 CRO 月报 · 评估 {rep['total_evaluated']} / "
                    f"建议 {len(rep['suggestions'])}",
                    rep['html'],
                )
            except Exception as e:
                print(f"email failed: {e}")
        return

    rep = evaluate_actions(window_days=args.window_days, since_days=args.since_days)
    print(render_summary_text(rep))
    if args.out:
        Path(args.out).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8',
        )
        print(f"Report saved: {args.out}")


if __name__ == '__main__':
    main()
