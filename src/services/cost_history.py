"""成本异动检测 (P13, 2026-05).

每天 daily_tasks 后落一份 (date, sku, total_cost) 到 SQLite 表 `cost_history`.
对比近 7 天最旧成本 vs 当日, 若 ↑ ≥ COST_JUMP_PCT (10%) 立即邮件告警.

避免供应商悄悄涨价致死线滑落 (PricingEngine 死线是 cost 的函数,
cost ↑ 会让现价瞬间跌入死线下方, 触发大批 reject/关广告).

API:
  - record_cost_snapshot(rows: [{sku, total_cost}], date=None)
  - detect_anomalies(jump_pct=0.10, lookback_days=7) -> [{sku, old_cost, new_cost, pct}]
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)
DEFAULT_DB = Path(__file__).resolve().parents[2] / 'ebay_collection.db'

COST_JUMP_PCT = 0.10
LOOKBACK_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_history (
    date TEXT NOT NULL,
    sku TEXT NOT NULL,
    total_cost REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (date, sku)
)
"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_cost_history_sku_date ON cost_history(sku, date)"


def ensure_schema(db_path: Path = DEFAULT_DB) -> None:
    c = sqlite3.connect(str(db_path))
    try:
        c.execute(_SCHEMA)
        c.execute(_INDEX)
        c.commit()
    finally:
        c.close()


def record_cost_snapshot(rows: Iterable[Dict[str, Any]], *,
                          db_path: Path = DEFAULT_DB,
                          date: Optional[str] = None) -> int:
    ensure_schema(db_path)
    date = date or datetime.now().strftime('%Y-%m-%d')
    now = datetime.now().isoformat()
    c = sqlite3.connect(str(db_path))
    try:
        cur = c.cursor()
        n = 0
        for r in rows:
            sku = r.get('sku')
            try:
                cost = float(r.get('total_cost') or 0)
            except (TypeError, ValueError):
                continue
            if not sku or cost <= 0:
                continue
            cur.execute(
                "INSERT OR REPLACE INTO cost_history (date, sku, total_cost, created_at) "
                "VALUES (?,?,?,?)",
                (date, sku, cost, now),
            )
            n += 1
        c.commit()
        logger.info(f"💰 cost_history 写入 {n} 行 (date={date})")
        return n
    finally:
        c.close()


def detect_anomalies(jump_pct: float = COST_JUMP_PCT,
                      lookback_days: int = LOOKBACK_DAYS,
                      *, db_path: Path = DEFAULT_DB,
                      today: Optional[str] = None,
                      enrich_with_floor: bool = False,
                      live_price_fn=None) -> List[Dict[str, Any]]:
    """对比每个 SKU 当日 cost vs lookback 内最旧 cost. 返回异动条目.

    P17: 若 enrich_with_floor=True 且提供 live_price_fn(sku) → 附加:
      - new_floor: 用新成本算出的死线 (PricingEngine.absolute_floor_price, ad_rate=None)
      - live_price: 当前 eBay 价
      - underwater: live_price < new_floor (现价已跌入新死线下)
      - drop_to_safe: 让现价 ≥ new_floor 需要再跟价多少 (>=0 表示需要再下杀)
    """
    ensure_schema(db_path)
    today = today or datetime.now().strftime('%Y-%m-%d')
    since = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        today_rows = {r['sku']: r['total_cost'] for r in c.execute(
            "SELECT sku, total_cost FROM cost_history WHERE date=?", (today,)
        )}
        if not today_rows:
            return []
        anomalies = []
        for sku, new_cost in today_rows.items():
            row = c.execute(
                "SELECT date, total_cost FROM cost_history "
                "WHERE sku=? AND date>=? AND date<? ORDER BY date ASC LIMIT 1",
                (sku, since, today),
            ).fetchone()
            if not row:
                continue
            old_cost = row['total_cost']
            if old_cost <= 0:
                continue
            pct = (new_cost - old_cost) / old_cost
            if pct >= jump_pct:
                entry = {
                    'sku': sku,
                    'old_cost': old_cost, 'new_cost': new_cost,
                    'jump_pct': round(pct, 4),
                    'baseline_date': row['date'], 'today': today,
                }
                if enrich_with_floor:
                    _enrich_floor(entry, live_price_fn)
                anomalies.append(entry)
        return sorted(anomalies, key=lambda x: -x['jump_pct'])
    finally:
        c.close()


def _enrich_floor(entry: Dict[str, Any], live_price_fn) -> None:
    try:
        from src.services.pricing_engine import PricingEngine
        from decimal import Decimal as _D
        new_floor = PricingEngine.absolute_floor_price(_D(str(entry['new_cost'])))
        entry['new_floor'] = float(new_floor)
        if live_price_fn:
            try:
                lp = live_price_fn(entry['sku'])
            except Exception:
                lp = None
            if lp:
                entry['live_price'] = float(lp)
                entry['underwater'] = float(lp) < float(new_floor)
                entry['drop_to_safe'] = max(0.0, float(new_floor) - float(lp))
    except Exception:
        pass


def render_alert_html(anomalies: List[Dict[str, Any]]) -> str:
    if not anomalies:
        return ''
    has_floor = any('new_floor' in a for a in anomalies)
    rows = ''
    for a in anomalies[:60]:
        floor_cells = ''
        if has_floor:
            uw = a.get('underwater')
            uw_color = '#b42318' if uw else '#067647'
            uw_text = '⚠️ 跌入死线' if uw else '安全'
            drop = a.get('drop_to_safe')
            drop_text = f'-${drop:.2f}' if drop and drop > 0 else '—'
            floor_cells = (
                f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">'
                f'${a.get("new_floor", 0):.2f}</td>'
                f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">'
                f'${a.get("live_price", 0):.2f}</td>'
                f'<td style="padding:4px 8px;border:1px solid #ddd;color:{uw_color};">{uw_text}</td>'
                f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;color:#b42318;">{drop_text}</td>'
            )
        rows += (
            f'<tr><td style="padding:4px 8px;border:1px solid #ddd;font-family:monospace;">{a["sku"]}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">${a["old_cost"]:.2f}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">${a["new_cost"]:.2f}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;color:#b42318;">+{a["jump_pct"]*100:.1f}%</td>'
            f'{floor_cells}'
            f'<td style="padding:4px 8px;border:1px solid #ddd;color:#999;font-size:11px;">{a["baseline_date"]}</td></tr>'
        )
    extra_th = ('<th style="padding:6px;border:1px solid #ddd;">新死线</th>'
                '<th style="padding:6px;border:1px solid #ddd;">现价</th>'
                '<th style="padding:6px;border:1px solid #ddd;">状态</th>'
                '<th style="padding:6px;border:1px solid #ddd;">需再降</th>') if has_floor else ''
    underwater_count = sum(1 for a in anomalies if a.get('underwater'))
    underwater_note = (f'<p style="color:#b42318;font-weight:bold;">'
                       f'⚠️ 其中 {underwater_count} 条 SKU 现价已跌入新死线下 — '
                       f'守门员会在下次跑跟价时强制拒改, 请人工处理.</p>') if underwater_count else ''
    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:900px;">
    <h2 style="color:#b42318;">💰 成本异动告警 — {len(anomalies)} 条 SKU 涨价 ≥ 10%</h2>
    {underwater_note}
    <p>请检查供应商页面是否调价. 若属实, 死线 (PricingEngine.absolute_floor_price)
       会等比上调, 部分现价可能瞬间跌入死线下方触发大批 reject/关广告.</p>
    <table style="border-collapse:collapse;font-size:13px;">
      <tr style="background:#f5f5f5;">
        <th style="padding:6px;border:1px solid #ddd;">SKU</th>
        <th style="padding:6px;border:1px solid #ddd;">原成本</th>
        <th style="padding:6px;border:1px solid #ddd;">当日成本</th>
        <th style="padding:6px;border:1px solid #ddd;">涨幅</th>
        {extra_th}
        <th style="padding:6px;border:1px solid #ddd;">基线日期</th>
      </tr>
      {rows}
    </table>
    </body></html>"""
