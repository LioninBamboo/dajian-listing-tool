"""分级动态 bid 优化 (P3, 2026-05).

按近 30 天表现把已推广 SKU 分三档, 自动调整 bid_percentage:

  HIGH  (impressions ≥ HIGH_IMP 且 (CTR ≥ HIGH_CTR 或 sold_qty ≥ 1)) → bid = HIGH_BID
  LOW   (impressions ≥ LOW_IMP 且 CTR < LOW_CTR 且 sold_qty == 0)     → bid = LOW_BID
  其余                                                              → bid = MID_BID (保持 5%)

每条调整都通过 PricingEngine.required_ad_rate_for(live_price, total_cost)
做现价撑得住校验 — 撑不住就降到现价支撑的最大 bid (5% 仍不撑就跳过).

CLI:
  python scripts/batch_smart_bid.py --dry-run            # 预览, 不改 bid
  python scripts/batch_smart_bid.py --apply              # 写入 eBay
  python scripts/batch_smart_bid.py --apply --email      # 写入 + 发邮件

落盘报告: logs/batch_smart_bid_<YYYYMMDD_HHMMSS>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.pricing_engine import PricingEngine

logger = logging.getLogger(__name__)
DB_PATH = PROJECT_ROOT / 'ebay_collection.db'
LOG_DIR = PROJECT_ROOT / 'logs'

# 分级阈值 (可调; 与 EbayAdService.get_ad_recommendations 思路一致)
HIGH_IMP = 500
HIGH_CTR = 0.03  # 3%
LOW_IMP = 300
LOW_CTR = 0.01  # 1%

HIGH_BID = 7.0  # %
MID_BID = 5.0
LOW_BID = 3.0

SAFETY_MARGIN = 0.05  # 5% 净利润安全边际
BID_DELTA_THRESHOLD = 0.5  # 与现 bid 差异 < 0.5% 视为无需调整


def _make_real_client():
    from src.clients.real_ebay_client import create_real_ebay_client

    return create_real_ebay_client(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))


def _fetch_cost_map() -> Dict[str, Dict[str, Any]]:
    """{sku: {total_cost, listing_id}}."""
    if not DB_PATH.exists():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT sku, listing_id, cost_breakdown FROM collected_products WHERE listing_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    for sku, lid, cost_json in rows:
        if not lid:
            continue
        try:
            cb = json.loads(cost_json) if cost_json else {}
            tc = float(cb.get('total_cost') or 0)
        except Exception:
            tc = 0.0
        out[sku] = {'total_cost': tc, 'listing_id': str(lid)}
    return out


def _load_active_experiments() -> List[Dict[str, Any]]:
    """P14: 拉所有 status='running' 的实验 + 各自的 SKU→arm 分配.

    返回格式: [{experiment_id, control_bid, variant_bid, assignments: {sku: arm}}].
    """
    try:
        from src.services import bid_experiment as bx
        exps = bx.list_experiments(status='running')
        out = []
        for e in exps:
            assignments = bx.get_assignments(e['experiment_id'])
            if not assignments:
                continue
            out.append({
                'experiment_id': e['experiment_id'],
                'control_bid': float(e['control_bid']),
                'variant_bid': float(e['variant_bid']),
                'assignments': assignments,
            })
        if out:
            n_skus = sum(len(e['assignments']) for e in out)
            logger.info(f"🧪 加载活跃实验 {len(out)} 个, 覆盖 {n_skus} SKU")
        return out
    except Exception as exc:
        logger.warning(f"加载活跃实验失败 (退化为纯 tier 模式): {exc}")
        return []


def _fetch_live_price(sku: str, real_client) -> Optional[float]:
    try:
        offer = real_client.get_offer_by_sku(sku)
    except Exception as exc:
        logger.warning(f"获取 offer {sku} 失败: {exc}")
        return None
    if not offer:
        return None
    try:
        return float(offer.get('pricingSummary', {}).get('price', {}).get('value') or 0)
    except Exception:
        return None


def classify(impressions: int, ctr: float, sold_qty: int) -> str:
    """返回 high / low / mid."""
    if impressions >= HIGH_IMP and (ctr >= HIGH_CTR or sold_qty >= 1):
        return 'high'
    if impressions >= LOW_IMP and ctr < LOW_CTR and sold_qty == 0:
        return 'low'
    return 'mid'


def desired_bid_for(tier: str) -> float:
    return {'high': HIGH_BID, 'low': LOW_BID, 'mid': MID_BID}[tier]


def cap_bid_by_floor(desired_bid_pct: float, live_price: float, total_cost: float,
                     safety: float = SAFETY_MARGIN) -> float:
    """返回现价支撑的最大 bid (%) 与 desired 取 min. < 0 表示连 0% 都亏."""
    if live_price <= 0 or total_cost <= 0:
        return desired_bid_pct  # 数据缺失 → 不动
    max_ad = PricingEngine.required_ad_rate_for(
        Decimal(str(live_price)), Decimal(str(total_cost)), Decimal(str(safety))
    )
    if max_ad < 0:
        return -1.0  # 不安全
    return min(desired_bid_pct, float(max_ad) * 100)


def build_plan(products_with_perf: List[Dict[str, Any]],
               ads_by_listing: Dict[str, Dict[str, Any]],
               cost_map: Dict[str, Dict[str, Any]],
               real_client=None,
               active_experiments: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """生成调整计划 (不调用 eBay 写接口).

    products_with_perf 每条至少含: sku, listing_id, impressions, ctr, sold_qty
    ads_by_listing: 来自 EbayAdService.fetch_all_ad_data()['ads'] (只含已推广)
    active_experiments (P14): [{experiment_id, control_bid, variant_bid, assignments: {sku: arm}}]
        若 SKU 命中任意活跃实验 → desired_bid 改为 arm bid; decision 标 'experiment'
        (仍受 cap_bid_by_floor 守门, 不安全则降级 skip_unsafe)
    """
    plan: List[Dict[str, Any]] = []
    # 把所有实验的 sku → (eid, arm, bid) 索引化, SKU 同时在多个实验只取第一个
    sku_to_exp: Dict[str, Dict[str, Any]] = {}
    for exp in (active_experiments or []):
        for sku, arm in (exp.get('assignments') or {}).items():
            if sku in sku_to_exp:
                continue
            arm_bid = exp['variant_bid'] if arm == 'variant' else exp['control_bid']
            sku_to_exp[sku] = {
                'experiment_id': exp['experiment_id'], 'arm': arm,
                'arm_bid': float(arm_bid),
            }

    for p in products_with_perf:
        sku = p.get('sku') or ''
        lid = str(p.get('listing_id') or '')
        if not sku or not lid:
            continue
        ad = ads_by_listing.get(lid)
        if not ad:
            continue  # 未推广不在本任务管理范围 (由 ad_recommendations 决定开/关)

        impressions = int(p.get('impressions') or 0)
        ctr = float(p.get('ctr') or 0)
        sold_qty = int(p.get('sold_qty') or 0)
        current_bid = float(ad.get('bid_percentage') or 5.0)
        cid = ad.get('campaign_id', '')

        tier = classify(impressions, ctr, sold_qty)
        desired = desired_bid_for(tier)

        # P14: 实验命中 → 用 arm bid 覆盖 tier desired
        exp_info = sku_to_exp.get(sku)
        if exp_info:
            desired = exp_info['arm_bid']

        cost_info = cost_map.get(sku) or {}
        total_cost = cost_info.get('total_cost', 0.0)
        live_price = 0.0
        if real_client and total_cost > 0:
            lp = _fetch_live_price(sku, real_client)
            if lp:
                live_price = lp

        capped = cap_bid_by_floor(desired, live_price, total_cost)
        if capped < 0:
            decision = 'skip_unsafe'
            new_bid = current_bid
            reason = f'现价 ${live_price:.2f} 连 0% 广告都亏 (cost ${total_cost:.2f})'
        elif abs(capped - current_bid) < BID_DELTA_THRESHOLD:
            decision = 'no_change'
            new_bid = current_bid
            reason = f'tier={tier} desired={desired:.1f}% capped={capped:.2f}% ≈ current'
        else:
            decision = 'experiment' if exp_info else 'adjust'
            new_bid = round(capped, 2)
            if exp_info:
                reason = (f'experiment={exp_info["experiment_id"]} arm={exp_info["arm"]} '
                          f'arm_bid={exp_info["arm_bid"]:.1f}% capped={capped:.2f}%')
            else:
                reason = f'tier={tier} desired={desired:.1f}% capped={capped:.2f}%'

        plan_item = {
            'sku': sku,
            'listing_id': lid,
            'campaign_id': cid,
            'tier': tier,
            'impressions': impressions,
            'ctr': round(ctr, 4),
            'sold_qty': sold_qty,
            'current_bid': round(current_bid, 2),
            'desired_bid': desired,
            'live_price': round(live_price, 2),
            'total_cost': round(total_cost, 2),
            'new_bid': round(new_bid, 2),
            'decision': decision,
            'reason': reason,
        }
        if exp_info:
            plan_item['experiment_id'] = exp_info['experiment_id']
            plan_item['experiment_arm'] = exp_info['arm']
        plan.append(plan_item)
    return plan


def apply_plan(plan: List[Dict[str, Any]], ad_service) -> Dict[str, Any]:
    """对 decision==adjust|experiment 的项调用 update_ad_bid."""
    ok, fail = 0, 0
    errors = []
    for item in plan:
        if item['decision'] not in ('adjust', 'experiment'):
            continue
        try:
            res = ad_service.update_ad_bid(item['campaign_id'], item['listing_id'], item['new_bid'])
            if isinstance(res, dict) and res.get('success'):
                ok += 1
                item['applied'] = True
            else:
                fail += 1
                err_msg = res.get('error') if isinstance(res, dict) else str(res)
                item['applied'] = False
                item['apply_error'] = err_msg
                errors.append({'sku': item['sku'], 'error': err_msg})
        except Exception as exc:
            fail += 1
            item['applied'] = False
            item['apply_error'] = str(exc)
            errors.append({'sku': item['sku'], 'error': str(exc)})
        time.sleep(0.2)  # eBay rate limit
    return {'applied_ok': ok, 'applied_failed': fail, 'errors': errors}


def summarize(plan: List[Dict[str, Any]]) -> Dict[str, int]:
    by_decision: Dict[str, int] = {}
    by_tier: Dict[str, int] = {}
    for it in plan:
        by_decision[it['decision']] = by_decision.get(it['decision'], 0) + 1
        by_tier[it['tier']] = by_tier.get(it['tier'], 0) + 1
    return {'by_decision': by_decision, 'by_tier': by_tier, 'total': len(plan)}


def render_email_html(report: Dict[str, Any]) -> str:
    s = report['summary']
    by_d = s['by_decision']
    by_t = s['by_tier']
    rows_adj = [it for it in report['plan'] if it['decision'] == 'adjust']
    detail = ''
    for it in rows_adj[:50]:
        diff = it['new_bid'] - it['current_bid']
        color = '#067647' if diff > 0 else '#b42318'
        applied = it.get('applied')
        status = '✅' if applied else ('❌' if applied is False else '🔍')
        detail += (
            f'<tr><td style="padding:4px 6px;border:1px solid #ddd;font-family:monospace;">{it["sku"]}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;">{it["tier"]}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;">{it["current_bid"]:.2f}%</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:right;font-weight:bold;color:{color};">{it["new_bid"]:.2f}%</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;text-align:center;">{status}</td>'
            f'<td style="padding:4px 6px;border:1px solid #ddd;font-size:12px;">{it["reason"]}</td></tr>'
        )
    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:760px;">
    <h2 style="color:#1a73e8;">🎯 智能 Bid 分级调整 - {report['timestamp'][:19].replace('T',' ')}</h2>
    <p>模式: <b>{'WRITE' if report['apply'] else 'DRY-RUN'}</b> · 总计 {s['total']} 条 ·
       调整 {by_d.get('adjust', 0)} · 无变化 {by_d.get('no_change', 0)} · 跳过(不安全) {by_d.get('skip_unsafe', 0)}</p>
    <p>分级: HIGH {by_t.get('high', 0)} / MID {by_t.get('mid', 0)} / LOW {by_t.get('low', 0)}</p>
    <table style="border-collapse:collapse;font-size:13px;">
      <tr style="background:#f5f5f5;"><th style="padding:6px;border:1px solid #ddd;">SKU</th>
        <th style="padding:6px;border:1px solid #ddd;">tier</th>
        <th style="padding:6px;border:1px solid #ddd;">old</th>
        <th style="padding:6px;border:1px solid #ddd;">new</th>
        <th style="padding:6px;border:1px solid #ddd;">applied</th>
        <th style="padding:6px;border:1px solid #ddd;">reason</th></tr>
      {detail or '<tr><td colspan=6 style="padding:8px;color:#999;">无调整项</td></tr>'}
    </table>
    </body></html>"""


def run(apply: bool = False, send_email: bool = False) -> Dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    from src.services.ebay_ad_service import EbayAdService
    from src.services.ebay_performance import (
        EbayPerformanceService, load_performance_cache, save_performance_cache
    )

    ad_svc = EbayAdService()
    real_client = _make_real_client()

    logger.info("加载广告数据...")
    ad_data = ad_svc.fetch_all_ad_data()
    ads_by_listing = ad_data.get('ads', {})
    if isinstance(ads_by_listing, list):
        ads_by_listing = {a['listing_id']: a for a in ads_by_listing if 'listing_id' in a}
    logger.info(f"已推广 listings: {len(ads_by_listing)}")

    logger.info("加载性能数据...")
    perf = load_performance_cache(max_age_hours=4)
    if not perf:
        perf = EbayPerformanceService().fetch_all_performance(traffic_days=30, sales_days=90)
        save_performance_cache(perf)
    traffic = perf.get('traffic', {})
    sales = perf.get('sales', {})
    by_listing_sales = sales.get('by_listing', {})
    by_sku_sales = sales.get('by_sku', {})

    cost_map = _fetch_cost_map()
    logger.info(f"DB cost 记录: {len(cost_map)}")

    products: List[Dict[str, Any]] = []
    for sku, info in cost_map.items():
        lid = info['listing_id']
        if lid not in ads_by_listing:
            continue
        t = traffic.get(lid, {})
        s = by_listing_sales.get(lid) or by_sku_sales.get(sku) or {}
        products.append({
            'sku': sku,
            'listing_id': lid,
            'impressions': t.get('impressions', 0),
            'ctr': t.get('ctr', 0),
            'sold_qty': s.get('qty', 0),
        })

    plan = build_plan(products, ads_by_listing, cost_map, real_client=real_client,
                       active_experiments=_load_active_experiments())
    summary = summarize(plan)
    apply_result: Dict[str, Any] = {'applied_ok': 0, 'applied_failed': 0, 'errors': []}
    if apply:
        n_apply = (summary['by_decision'].get('adjust', 0)
                    + summary['by_decision'].get('experiment', 0))
        logger.info(f"应用 {n_apply} 条 bid 调整 (含实验)...")
        apply_result = apply_plan(plan, ad_svc)

    report = {
        'timestamp': datetime.now().isoformat(),
        'apply': apply,
        'summary': summary,
        'apply_result': apply_result,
        'plan': plan,
    }

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOG_DIR / f"batch_smart_bid_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"报告: {out_path}")

    if send_email:
        try:
            from src.utils.email_sender import send_email as _send
            subject = (f"🎯 Smart Bid {'应用' if apply else '预览'} - "
                       f"调整{summary['by_decision'].get('adjust', 0)} / 总{summary['total']}")
            _send(subject, render_email_html(report))
        except Exception as exc:
            logger.warning(f"邮件发送失败: {exc}")

    return report


def _parse_args():
    ap = argparse.ArgumentParser(description='分级动态 bid 优化')
    ap.add_argument('--apply', action='store_true', help='实际写入 eBay (默认 dry-run)')
    ap.add_argument('--dry-run', action='store_true', help='只预览, 不调用写接口')
    ap.add_argument('--email', action='store_true', help='发送结果邮件')
    return ap.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    apply_flag = args.apply and not args.dry_run
    rpt = run(apply=apply_flag, send_email=args.email)
    s = rpt['summary']
    print(f"\nDONE — total={s['total']} adjust={s['by_decision'].get('adjust', 0)} "
          f"no_change={s['by_decision'].get('no_change', 0)} "
          f"skip_unsafe={s['by_decision'].get('skip_unsafe', 0)}")
    if apply_flag:
        ar = rpt['apply_result']
        print(f"applied_ok={ar['applied_ok']} applied_failed={ar['applied_failed']}")
