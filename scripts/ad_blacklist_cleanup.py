"""广告黑名单自动清理 (P7, 2026-05).

每天对 reason=auto_off_streak 的 SKU 复核:
  - 拉现网 live_price + DB cost
  - 用 PricingEngine.required_ad_rate_for(price, cost) 反算
  - max_ad >= 0.05 → mark_safe_check(True), 累计 safe_streak
  - max_ad < 0.05  → mark_safe_check(False), 重置
  - safe_streak >= AUTO_REMOVE_SAFE_DAYS (默认 7) → auto_remove

manual 黑名单永不动. 报告: logs/ad_blacklist_cleanup_<ts>.json

CLI:
    python scripts/ad_blacklist_cleanup.py --dry-run
    python scripts/ad_blacklist_cleanup.py --apply --email
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)
DB_PATH = PROJECT_ROOT / 'ebay_collection.db'
LOG_DIR = PROJECT_ROOT / 'logs'
SAFETY_MARGIN = 0.05
TARGET_AD_RATE = 0.05  # 5% 广告; max_ad >= 此值才算 safe


def _make_real_client():
    from src.clients.real_ebay_client import create_real_ebay_client

    return create_real_ebay_client(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))


def _fetch_cost(sku: str, db_path: Path = DB_PATH) -> Optional[float]:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT cost_breakdown FROM collected_products WHERE sku=? LIMIT 1",
            (sku,),
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        cb = json.loads(row[0])
        return float(cb.get('total_cost') or 0) or None
    except Exception:
        return None


def _fetch_live_price(sku: str, real_client) -> Optional[float]:
    try:
        offer = real_client.get_offer_by_sku(sku)
    except Exception as exc:
        logger.warning(f"获取 {sku} live price 失败: {exc}")
        return None
    if not offer:
        return None
    try:
        return float(offer.get('pricingSummary', {}).get('price', {}).get('value') or 0) or None
    except Exception:
        return None


def evaluate_recovery(sku: str, entry: Dict[str, Any], live_price: Optional[float],
                       cost: Optional[float], target_ad_rate: float = TARGET_AD_RATE,
                       safety: float = SAFETY_MARGIN) -> Dict[str, Any]:
    """决定一条 SKU 是 safe / unsafe / no_data."""
    from src.services.pricing_engine import PricingEngine

    if cost is None or cost <= 0 or live_price is None or live_price <= 0:
        return {'sku': sku, 'is_safe': None, 'reason': 'no_data',
                'live_price': live_price, 'cost': cost,
                'safe_streak_before': int(entry.get('safe_streak', 0))}

    max_ad = PricingEngine.required_ad_rate_for(
        Decimal(str(live_price)), Decimal(str(cost)), Decimal(str(safety))
    )
    is_safe = float(max_ad) >= target_ad_rate
    return {
        'sku': sku, 'is_safe': bool(is_safe),
        'reason': 'recovered' if is_safe else 'still_unsafe',
        'live_price': live_price, 'cost': cost,
        'max_ad_rate': float(max_ad), 'target_ad_rate': target_ad_rate,
        'safe_streak_before': int(entry.get('safe_streak', 0)),
    }


def run(apply: bool = False, send_email: bool = False,
        threshold_days: int = None) -> Dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    from src.services import ad_blacklist

    threshold_days = threshold_days or ad_blacklist.AUTO_REMOVE_SAFE_DAYS
    bl = ad_blacklist.list_all()
    auto_skus = [s for s, e in bl.items() if e.get('reason') == 'auto_off_streak']
    logger.info(f"待复核 auto_off_streak SKU: {len(auto_skus)} (manual 永不清理)")

    real_client = _make_real_client() if auto_skus else None
    decisions: List[Dict[str, Any]] = []
    removed = []
    for sku in auto_skus:
        entry = bl[sku]
        cost = _fetch_cost(sku)
        live_price = _fetch_live_price(sku, real_client) if real_client else None
        d = evaluate_recovery(sku, entry, live_price, cost)
        if apply and d['is_safe'] is not None:
            ad_blacklist.mark_safe_check(sku, d['is_safe'])
            if d['is_safe']:
                if ad_blacklist.auto_remove_if_recovered(sku, threshold=threshold_days):
                    d['removed'] = True
                    removed.append(sku)
        decisions.append(d)

    summary = {
        'total': len(decisions),
        'safe': sum(1 for d in decisions if d['is_safe'] is True),
        'unsafe': sum(1 for d in decisions if d['is_safe'] is False),
        'no_data': sum(1 for d in decisions if d['is_safe'] is None),
        'removed': len(removed),
    }
    report = {
        'timestamp': datetime.now().isoformat(),
        'apply': apply,
        'threshold_days': threshold_days,
        'summary': summary,
        'removed_skus': removed,
        'decisions': decisions,
    }

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOG_DIR / f"ad_blacklist_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"报告: {out_path} | safe={summary['safe']} unsafe={summary['unsafe']} "
                f"no_data={summary['no_data']} removed={summary['removed']}")

    if send_email and (summary['removed'] or summary['safe']):
        try:
            from src.utils.email_sender import send_email as _send
            html = _render_html(report)
            subject = f"♻️ 广告黑名单清理 - 移出 {summary['removed']} / 安全 {summary['safe']}"
            _send(subject, html)
        except Exception as exc:
            logger.warning(f"邮件发送失败: {exc}")

    return report


def _render_html(report: Dict[str, Any]) -> str:
    s = report['summary']
    rows = ''
    for d in report['decisions'][:80]:
        color = ('#067647' if d['is_safe'] else
                 '#b42318' if d['is_safe'] is False else '#999')
        removed = '♻️' if d.get('removed') else ''
        rows += (
            f'<tr><td style="padding:4px 6px;border:1px solid #ddd;font-family:monospace;">{d["sku"]}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;color:{color};">{d["reason"]}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">${d.get("live_price") or "—"}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">${d.get("cost") or "—"}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">{(d.get("max_ad_rate") or 0)*100:.2f}%</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">{d.get("safe_streak_before", 0)}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:center;">{removed}</td></tr>'
        )
    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:760px;">
    <h2 style="color:#067647;">♻️ 广告黑名单自动清理 - {report['timestamp'][:19].replace('T',' ')}</h2>
    <p>评估 {s['total']} · 现在安全 {s['safe']} · 仍不安全 {s['unsafe']} ·
       数据缺失 {s['no_data']} · <b>移出黑名单 {s['removed']}</b>
       (阈值: 连续 {report['threshold_days']} 天安全)</p>
    <table style="border-collapse:collapse;font-size:12px;">
      <tr style="background:#f5f5f5;">
        <th style="padding:6px;border:1px solid #ddd;">SKU</th>
        <th style="padding:6px;border:1px solid #ddd;">状态</th>
        <th style="padding:6px;border:1px solid #ddd;">现价</th>
        <th style="padding:6px;border:1px solid #ddd;">成本</th>
        <th style="padding:6px;border:1px solid #ddd;">max_ad</th>
        <th style="padding:6px;border:1px solid #ddd;">连续安全(前)</th>
        <th style="padding:6px;border:1px solid #ddd;">移出</th>
      </tr>
      {rows or '<tr><td colspan=7 style="padding:8px;color:#999;">无 auto_off_streak SKU</td></tr>'}
    </table>
    </body></html>"""


def _parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--email', action='store_true')
    ap.add_argument('--threshold-days', type=int, default=None)
    return ap.parse_args()


if __name__ == '__main__':
    a = _parse_args()
    r = run(apply=a.apply and not a.dry_run, send_email=a.email,
            threshold_days=a.threshold_days)
    s = r['summary']
    print(f"\nDONE — total={s['total']} safe={s['safe']} unsafe={s['unsafe']} "
          f"no_data={s['no_data']} removed={s['removed']}")
