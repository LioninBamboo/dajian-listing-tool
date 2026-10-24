"""CRO 北极星指标告警 — 7d 滚动均值跌幅 / 当日恶化占比.

每天 daily_tasks 之后跑一次. 触发条件:
  - 7d avg_cro_score 比"上一个 7d 窗口"跌 ≥ 5 分
  - 或 worsened SKU ≥ today_total * 20% (至少 3 个)
两者任一满足 → 邮件告警 (subject 带 🚨).

调度建议: 10:30 (在 cro_consume 之后, 让消费效果体现)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'

DROP_THRESHOLD = 5.0
WORSENED_RATIO = 0.20
WORSENED_MIN = 3


def _avg_score(db: Path, start: str, end: str) -> Optional[float]:
    with sqlite3.connect(str(db)) as c:
        cur = c.execute(
            "SELECT AVG(cro_score) FROM cro_snapshots "
            "WHERE snapshot_date BETWEEN ? AND ?",
            (start, end),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


def _delta_today_vs_yesterday(db: Path) -> Dict[str, Any]:
    """直接读 cro_snapshots 算 today vs yesterday 的 worsened 数."""
    today = date.today().isoformat()
    yest = (date.today() - timedelta(days=1)).isoformat()
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        a = {r['sku']: r for r in c.execute(
            "SELECT sku, cro_score FROM cro_snapshots WHERE snapshot_date = ?", (today,)
        ).fetchall()}
        b = {r['sku']: r for r in c.execute(
            "SELECT sku, cro_score FROM cro_snapshots WHERE snapshot_date = ?", (yest,)
        ).fetchall()}
    worsened = []
    improved = []
    for sku, ra in a.items():
        rb = b.get(sku)
        if not rb:
            continue
        d = ra['cro_score'] - rb['cro_score']
        if d <= -5:
            worsened.append(sku)
        elif d >= 5:
            improved.append(sku)
    return {
        'today_total': len(a),
        'worsened': worsened,
        'improved': improved,
    }


def evaluate(db_path: Optional[Path] = None) -> Dict[str, Any]:
    db = Path(db_path) if db_path else DEFAULT_DB
    today = date.today()
    cur_end = today.isoformat()
    cur_start = (today - timedelta(days=6)).isoformat()
    prev_end = (today - timedelta(days=7)).isoformat()
    prev_start = (today - timedelta(days=13)).isoformat()
    cur_avg = _avg_score(db, cur_start, cur_end)
    prev_avg = _avg_score(db, prev_start, prev_end)
    drop = (prev_avg - cur_avg) if (cur_avg is not None and prev_avg is not None) else 0.0

    delta = _delta_today_vs_yesterday(db)
    worsened_n = len(delta['worsened'])
    today_total = delta['today_total'] or 1
    worsened_ratio = worsened_n / today_total

    triggers = []
    if cur_avg is not None and prev_avg is not None and drop >= DROP_THRESHOLD:
        triggers.append(f"7d 平均 CRO 分跌 {drop:.1f} 分 ({prev_avg:.1f} → {cur_avg:.1f})")
    if worsened_n >= WORSENED_MIN and worsened_ratio >= WORSENED_RATIO:
        triggers.append(
            f"今日恶化 {worsened_n}/{today_total} = {worsened_ratio:.0%} "
            f"(阈值 {WORSENED_RATIO:.0%}, 最少 {WORSENED_MIN})"
        )

    return {
        'today': cur_end,
        'cur_7d_avg': cur_avg,
        'prev_7d_avg': prev_avg,
        'drop': drop,
        'today_total': today_total,
        'worsened': delta['worsened'],
        'improved': delta['improved'],
        'worsened_ratio': worsened_ratio,
        'triggers': triggers,
        'should_alert': bool(triggers),
    }


def render_html(rep: Dict[str, Any]) -> str:
    triggers_html = ''.join(f'<li>{t}</li>' for t in rep['triggers']) or '<li>无</li>'
    worsened_list = ', '.join(rep['worsened'][:30]) + (' ...' if len(rep['worsened']) > 30 else '')
    return f'''
    <html><body style="font-family:Arial,sans-serif;padding:20px;">
    <h2 style="color:#b42318;">🚨 CRO 转化率告警 — {rep['today']}</h2>
    <h3>触发条件</h3>
    <ul>{triggers_html}</ul>
    <h3>关键指标</h3>
    <table style="border-collapse:collapse;font-size:13px;">
      <tr><td style="padding:6px 10px;border:1px solid #ddd;">本周 7d 平均 CRO 分</td>
          <td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;">{(rep['cur_7d_avg'] or 0):.1f}</td></tr>
      <tr><td style="padding:6px 10px;border:1px solid #ddd;">上周 7d 平均 CRO 分</td>
          <td style="padding:6px 10px;border:1px solid #ddd;">{(rep['prev_7d_avg'] or 0):.1f}</td></tr>
      <tr><td style="padding:6px 10px;border:1px solid #ddd;">跌幅</td>
          <td style="padding:6px 10px;border:1px solid #ddd;color:#b42318;font-weight:bold;">{rep['drop']:.1f} 分</td></tr>
      <tr><td style="padding:6px 10px;border:1px solid #ddd;">今日恶化 SKU</td>
          <td style="padding:6px 10px;border:1px solid #ddd;color:#b42318;">{len(rep['worsened'])} / {rep['today_total']} ({rep['worsened_ratio']:.0%})</td></tr>
      <tr><td style="padding:6px 10px;border:1px solid #ddd;">今日改善 SKU</td>
          <td style="padding:6px 10px;border:1px solid #ddd;color:#067647;">{len(rep['improved'])}</td></tr>
    </table>
    <h3>恶化 SKU (前 30)</h3>
    <p style="font-family:monospace;font-size:12px;color:#344054;">{worsened_list or '(无)'}</p>
    <hr>
    <p style="color:#999;font-size:12px;">建议: 打开「竞争监控 → 🎯 转化率诊断」查看 P1 动作清单, 重点排查最近改价/改图是否反向.</p>
    </body></html>
    '''


def main():
    p = argparse.ArgumentParser(description='CRO sentinel — alert on 7d drop / worsened ratio')
    p.add_argument('--email', action='store_true', help='Send email if triggered')
    p.add_argument('--out', help='Write json report to path')
    args = p.parse_args()
    rep = evaluate()
    print(json.dumps({k: v for k, v in rep.items()
                      if k not in ('worsened', 'improved')}, indent=2))
    if rep['triggers']:
        print('TRIGGERED:', '; '.join(rep['triggers']))
    if args.out:
        Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
    else:
        # 默认写一份 logs/cro_sentinel_<date>.json 给 dashboard 读
        default_out = (PROJECT_ROOT / 'logs'
                       / f"cro_sentinel_{rep['today']}.json")
        default_out.parent.mkdir(parents=True, exist_ok=True)
        default_out.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                               encoding='utf-8')
    if args.email and rep['should_alert']:
        try:
            from src.utils.email_sender import send_email
            subject = f"🚨 CRO 跳水告警 - {rep['today']} - 跌{rep['drop']:.1f}分 / 恶化{len(rep['worsened'])}个"
            send_email(subject, render_html(rep))
            print('Email sent.')
        except Exception as e:
            print(f'Email failed: {e}')


if __name__ == '__main__':
    main()
