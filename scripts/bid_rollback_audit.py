"""Smart Bid 调整效果回溯 (P5, 2026-05).

读取 7 天前的 `logs/batch_smart_bid_*.json`, 找出当时 **从 5%(或更低) 提升到
7%** 的 SKU. 拉今天的最新 30d 性能, 与提升前比较:

  - 若提升后 CTR 没改善 (绝对涨幅 < CTR_IMPROVEMENT) 且销量未涨 → 回退到 MID_BID
  - 否则保留

每条回退操作走 `EbayAdService.update_ad_bid` (eBay 写); 同样不写超过守门员死线.

CLI:
    python scripts/bid_rollback_audit.py --dry-run
    python scripts/bid_rollback_audit.py --apply --email

落盘报告: logs/bid_rollback_audit_<ts>.json
被 scheduler 周二 11:00 调度 (在 smart_bid 09:00 之后, 留 1h 数据写入余量
不实际, 实际是 7 天 lag, 每周触发即可).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)
LOG_DIR = PROJECT_ROOT / 'logs'

LOOKBACK_DAYS = 7
CTR_IMPROVEMENT = 0.005  # 绝对 CTR 涨幅 < 0.5% 视为没改善
MID_BID = 5.0  # 与 batch_smart_bid.MID_BID 保持一致


def find_baseline_report(lookback_days: int = LOOKBACK_DAYS,
                          log_dir: Optional[Path] = None) -> Optional[Path]:
    """找 lookback_days 天前最早的 smart_bid 报告 (作为回顾基线)."""
    log_dir = log_dir or LOG_DIR
    if not log_dir.exists():
        return None
    target = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y%m%d')
    matches = sorted(log_dir.glob(f'batch_smart_bid_{target}_*.json'))
    if matches:
        return matches[0]
    # 退化: 取离 target 日期最近的一份 (前后 ±2 天)
    all_files = sorted(log_dir.glob('batch_smart_bid_*.json'))
    if not all_files:
        return None
    target_dt = datetime.now() - timedelta(days=lookback_days)
    best = min(
        all_files,
        key=lambda p: abs(_parse_ts_from_filename(p) - target_dt),
        default=None,
    )
    if best is None:
        return None
    if abs(_parse_ts_from_filename(best) - target_dt) > timedelta(days=2):
        return None
    return best


def _parse_ts_from_filename(p: Path) -> datetime:
    """batch_smart_bid_YYYYMMDD_HHMMSS.json → datetime."""
    try:
        stem = p.stem  # batch_smart_bid_20260427_100000
        parts = stem.split('_')
        return datetime.strptime(parts[-2] + parts[-1], '%Y%m%d%H%M%S')
    except Exception:
        return datetime.min


def extract_bid_raises(baseline_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从基线报告中提取 '从 ≤MID 提升到 >MID' 的应用项."""
    out = []
    for it in baseline_report.get('plan', []):
        if it.get('decision') != 'adjust':
            continue
        if not it.get('applied'):
            continue
        if it.get('current_bid', 0) <= MID_BID and it.get('new_bid', 0) > MID_BID:
            out.append(it)
    return out


def evaluate_rollback(baseline_item: Dict[str, Any],
                      now_perf: Dict[str, Any],
                      ctr_improvement: float = CTR_IMPROVEMENT) -> Dict[str, Any]:
    """决定一条 SKU 是否需要回退.

    Args:
        baseline_item: 基线报告中的 plan item (有 ctr / sold_qty / current_bid / new_bid)
        now_perf: {'impressions','ctr','sold_qty'} 现在最新值
    Returns:
        {'sku', 'decision' ∈ {keep, rollback, no_data}, 'reason', 'old_bid_target'}
    """
    sku = baseline_item['sku']
    old_ctr = float(baseline_item.get('ctr') or 0)
    old_sold = int(baseline_item.get('sold_qty') or 0)
    new_ctr = float(now_perf.get('ctr') or 0)
    new_sold = int(now_perf.get('sold_qty') or 0)
    impr = int(now_perf.get('impressions') or 0)

    if impr < 50:
        return {
            'sku': sku, 'decision': 'no_data',
            'reason': f'7天后 impressions={impr}, 数据不足判断',
            'old_bid_target': MID_BID,
            'old_ctr': old_ctr, 'new_ctr': new_ctr,
            'old_sold_qty': old_sold, 'new_sold_qty': new_sold,
            'current_bid': baseline_item.get('new_bid'),
        }

    ctr_delta = new_ctr - old_ctr
    sold_delta = new_sold - old_sold
    if ctr_delta < ctr_improvement and sold_delta <= 0:
        return {
            'sku': sku, 'decision': 'rollback',
            'reason': (f'CTR Δ={ctr_delta:+.3%} < {ctr_improvement:.1%}, '
                       f'销量 Δ={sold_delta:+d} → bid 提升无效, 回退到 {MID_BID}%'),
            'old_bid_target': MID_BID,
            'old_ctr': old_ctr, 'new_ctr': new_ctr,
            'old_sold_qty': old_sold, 'new_sold_qty': new_sold,
            'current_bid': baseline_item.get('new_bid'),
        }
    return {
        'sku': sku, 'decision': 'keep',
        'reason': (f'CTR Δ={ctr_delta:+.3%}, 销量 Δ={sold_delta:+d} → 提升有效'),
        'old_bid_target': baseline_item.get('new_bid'),
        'old_ctr': old_ctr, 'new_ctr': new_ctr,
        'old_sold_qty': old_sold, 'new_sold_qty': new_sold,
        'current_bid': baseline_item.get('new_bid'),
    }


def apply_rollbacks(decisions: List[Dict[str, Any]], baseline_items: List[Dict[str, Any]],
                    ad_service) -> Dict[str, int]:
    """对 decision==rollback 的项调 update_ad_bid."""
    by_sku = {it['sku']: it for it in baseline_items}
    ok, fail = 0, 0
    for d in decisions:
        if d['decision'] != 'rollback':
            continue
        item = by_sku.get(d['sku'])
        if not item:
            continue
        try:
            res = ad_service.update_ad_bid(
                item['campaign_id'], item['listing_id'], d['old_bid_target']
            )
            if isinstance(res, dict) and res.get('success'):
                ok += 1
                d['applied'] = True
            else:
                fail += 1
                d['applied'] = False
                d['apply_error'] = res.get('error') if isinstance(res, dict) else str(res)
        except Exception as exc:
            fail += 1
            d['applied'] = False
            d['apply_error'] = str(exc)
        time.sleep(0.2)
    return {'rolled_back_ok': ok, 'rolled_back_failed': fail}


def render_email_html(report: Dict[str, Any]) -> str:
    decisions = report['decisions']
    n_total = len(decisions)
    n_rb = sum(1 for d in decisions if d['decision'] == 'rollback')
    n_keep = sum(1 for d in decisions if d['decision'] == 'keep')
    n_nd = sum(1 for d in decisions if d['decision'] == 'no_data')

    rows = ''
    for d in decisions[:80]:
        color = {'rollback': '#b42318', 'keep': '#067647', 'no_data': '#999'}[d['decision']]
        applied = d.get('applied')
        status = '↩️' if applied else ('❌' if applied is False else ('✅' if d['decision'] == 'keep' else '🔍'))
        rows += (
            f'<tr><td style="padding:4px 6px;border:1px solid #ddd;font-family:monospace;">{d["sku"]}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;color:{color};font-weight:bold;">{d["decision"]}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">{d.get("current_bid", 0):.1f}%</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">{d.get("old_bid_target", 0):.1f}%</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">{d.get("old_ctr", 0):.2%} → {d.get("new_ctr", 0):.2%}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">{d.get("old_sold_qty", 0)} → {d.get("new_sold_qty", 0)}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:center;">{status}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;font-size:12px;">{d["reason"]}</td></tr>'
        )

    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:820px;">
    <h2 style="color:#1a73e8;">↩️ Smart Bid 7 天回溯 - {report['timestamp'][:19].replace('T', ' ')}</h2>
    <p>基线报告: <code>{report.get('baseline_file', 'N/A')}</code> · 评估 {n_total} 条 SKU</p>
    <p>回退 {n_rb} · 保留 {n_keep} · 数据不足 {n_nd}</p>
    <table style="border-collapse:collapse;font-size:12px;">
      <tr style="background:#f5f5f5;">
        <th style="padding:6px;border:1px solid #ddd;">SKU</th>
        <th style="padding:6px;border:1px solid #ddd;">决定</th>
        <th style="padding:6px;border:1px solid #ddd;">当前 bid</th>
        <th style="padding:6px;border:1px solid #ddd;">目标 bid</th>
        <th style="padding:6px;border:1px solid #ddd;">CTR (老→新)</th>
        <th style="padding:6px;border:1px solid #ddd;">销量 (老→新)</th>
        <th style="padding:6px;border:1px solid #ddd;">写入</th>
        <th style="padding:6px;border:1px solid #ddd;">原因</th>
      </tr>
      {rows or '<tr><td colspan=8 style="padding:8px;color:#999;">无 7 天前的 bid 提升记录</td></tr>'}
    </table>
    </body></html>"""


def run(apply: bool = False, send_email: bool = False,
        lookback_days: int = LOOKBACK_DAYS) -> Dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    baseline_path = find_baseline_report(lookback_days)
    if not baseline_path:
        logger.warning(f"找不到 {lookback_days} 天前的 batch_smart_bid 报告, 跳过")
        report = {
            'timestamp': datetime.now().isoformat(), 'apply': apply,
            'baseline_file': None, 'decisions': [],
            'apply_result': {'rolled_back_ok': 0, 'rolled_back_failed': 0},
        }
    else:
        logger.info(f"基线报告: {baseline_path}")
        baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
        raises = extract_bid_raises(baseline)
        logger.info(f"基线中提升 bid 的 SKU: {len(raises)}")

        from src.services.ebay_performance import (
            EbayPerformanceService, load_performance_cache, save_performance_cache
        )
        perf = load_performance_cache(max_age_hours=4)
        if not perf:
            perf = EbayPerformanceService().fetch_all_performance(traffic_days=30, sales_days=90)
            save_performance_cache(perf)
        traffic = perf.get('traffic', {})
        sales = perf.get('sales', {})
        by_listing = sales.get('by_listing', {})
        by_sku = sales.get('by_sku', {})

        decisions = []
        for it in raises:
            lid = str(it.get('listing_id') or '')
            sku = it.get('sku', '')
            t = traffic.get(lid, {})
            s = by_listing.get(lid) or by_sku.get(sku) or {}
            now_perf = {
                'impressions': t.get('impressions', 0),
                'ctr': t.get('ctr', 0),
                'sold_qty': s.get('qty', 0),
            }
            decisions.append(evaluate_rollback(it, now_perf))

        apply_result = {'rolled_back_ok': 0, 'rolled_back_failed': 0}
        if apply:
            from src.services.ebay_ad_service import EbayAdService
            apply_result = apply_rollbacks(decisions, raises, EbayAdService())

        report = {
            'timestamp': datetime.now().isoformat(), 'apply': apply,
            'baseline_file': str(baseline_path.name),
            'decisions': decisions, 'apply_result': apply_result,
        }

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOG_DIR / f"bid_rollback_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"报告: {out_path}")

    if send_email:
        try:
            from src.utils.email_sender import send_email as _send
            n_rb = sum(1 for d in report['decisions'] if d['decision'] == 'rollback')
            subject = (f"↩️ Bid Rollback {'应用' if apply else '预览'} - "
                       f"回退{n_rb}/总{len(report['decisions'])}")
            _send(subject, render_email_html(report))
        except Exception as exc:
            logger.warning(f"邮件发送失败: {exc}")

    return report


def _parse_args():
    ap = argparse.ArgumentParser(description='Smart Bid 7 天回溯')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--email', action='store_true')
    ap.add_argument('--lookback-days', type=int, default=LOOKBACK_DAYS)
    return ap.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    apply_flag = args.apply and not args.dry_run
    rpt = run(apply=apply_flag, send_email=args.email, lookback_days=args.lookback_days)
    decs = rpt['decisions']
    n_rb = sum(1 for d in decs if d['decision'] == 'rollback')
    print(f"\nDONE — total={len(decs)} rollback={n_rb} "
          f"keep={sum(1 for d in decs if d['decision'] == 'keep')} "
          f"no_data={sum(1 for d in decs if d['decision'] == 'no_data')}")
