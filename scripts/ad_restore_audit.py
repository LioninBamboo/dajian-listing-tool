"""广告自动恢复审计 (2026-05).

闭环 Phase 3: `repricing_guard.precheck_price` 在触底时自动 `delete_ad` 关广告.
本脚本是反向流程: 当条件改善 (供应商降价 / 跟价后利润空间恢复 / 市场价回升)
时, 把先前被自动关掉的广告重新打开.

判定规则:
  对每个 PUBLISHED 且当前 listing 没有活跃广告的 SKU,
  - 读 cost_breakdown.total_dajian_cost
  - 读 eBay 当前 offer.price (现网真实价, 不信任本地)
  - 计算 PricingEngine.required_ad_rate_for(price, cost, safety_margin=DEFAULT_SAFETY)
  - 若 ≥ TARGET_AD_RATE (默认 5%) → 该 listing 现价能撑得起 5% 广告 → 重新开广告

CLI:
  python scripts/ad_restore_audit.py                     # dry-run, 仅打印
  python scripts/ad_restore_audit.py --apply             # 真的开广告
  python scripts/ad_restore_audit.py --apply --email     # 开广告 + 发邮件
  python scripts/ad_restore_audit.py --safety 0.08       # 自定义利润缓冲
  python scripts/ad_restore_audit.py --target-bid 5.0    # 自定义目标 bid

调度: scheduler_daemon 09:30 daily_tasks 完成后跑一次.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from src.services.pricing_engine import PricingEngine

LOG_DIR = ROOT / 'logs'
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / 'ad_restore_audit.log', encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger('ad_restore_audit')

DB_PATH = str(ROOT / 'ebay_collection.db')

# ─── 数据加载 ──────────────────────────────────────────

def load_published_skus() -> List[Dict[str, Any]]:
    """读取所有 PUBLISHED 且有 cost_breakdown + listing_id 的 SKU."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT sku, title, listing_id, cost_breakdown
        FROM collected_products
        WHERE status = 'PUBLISHED'
          AND listing_id IS NOT NULL AND listing_id != ''
          AND cost_breakdown IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            cb = json.loads(r['cost_breakdown']) if r['cost_breakdown'] else {}
        except Exception:
            cb = {}
        tc = cb.get('total_dajian_cost')
        if not tc or float(tc) <= 0:
            continue
        out.append({
            'sku': r['sku'],
            'title': r['title'],
            'listing_id': str(r['listing_id']),
            'total_cost': float(tc),
        })
    return out


def _offer_price(offer: Dict[str, Any]) -> Optional[float]:
    value = offer.get('pricingSummary', {}).get('price', {}).get('value')
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_live_listing_state(oauth, sku: str,
                             expected_listing_id: Optional[str] = None) -> Dict[str, Any]:
    """读取 SKU 当前 live offer 状态，避免把本地过期 listing 当作可恢复目标."""
    token = oauth.get_valid_token()
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        r = requests.get(
            f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}",
            headers=headers, timeout=30, verify=False,
        )
        if r.status_code != 200:
            return {'state': 'error', 'reason': f'offer API HTTP {r.status_code}'}

        offers = r.json().get('offers', [])
        published = []
        for offer in offers:
            if offer.get('status') != 'PUBLISHED':
                continue
            price = _offer_price(offer)
            published.append({
                'listing_id': str(offer.get('listingId', '') or ''),
                'marketplace_id': offer.get('marketplaceId'),
                'price': price,
            })

        preferred = [
            o for o in published
            if o['marketplace_id'] == 'EBAY_US'
        ] or published

        if expected_listing_id:
            expected_listing_id = str(expected_listing_id)
            for offer in preferred:
                if offer['listing_id'] == expected_listing_id and offer['price'] is not None:
                    return {
                        'state': 'live',
                        'listing_id': expected_listing_id,
                        'live_price': offer['price'],
                    }

            live_listing_ids = [o['listing_id'] for o in preferred if o['listing_id']]
            if live_listing_ids:
                return {
                    'state': 'not_live',
                    'reason': f'本地 listing_id={expected_listing_id} 不在 eBay 当前 PUBLISHED offers 中',
                    'live_listing_ids': live_listing_ids,
                }
            return {
                'state': 'not_live',
                'reason': f'SKU={sku} 当前无 PUBLISHED offer',
                'live_listing_ids': [],
            }

        for offer in preferred:
            if offer['price'] is not None:
                return {
                    'state': 'live',
                    'listing_id': offer['listing_id'],
                    'live_price': offer['price'],
                }
    except Exception as e:
        log.warning(f"  {sku}: fetch_live_listing_state 失败: {e}")
        return {'state': 'error', 'reason': str(e)}

    return {'state': 'not_live', 'reason': f'SKU={sku} 无可用 live price', 'live_listing_ids': []}


def fetch_live_price(oauth, sku: str) -> Optional[float]:
    """兼容旧调用方: 返回任意 live offer 的价格."""
    state = fetch_live_listing_state(oauth, sku)
    if state.get('state') == 'live':
        return state.get('live_price')
    return None


# ─── 广告决策 ──────────────────────────────────────────

def evaluate_sku(sku_info: Dict[str, Any], live_price: float,
                 target_bid: float, safety_margin: float) -> Dict[str, Any]:
    """评估单 SKU 是否应该重开广告.

    Returns:
        dict with keys:
          decision: 'restore' | 'skip_unsafe' | 'skip_low_margin'
          live_price, total_cost, required_ad_rate, headroom
    """
    cost = sku_info['total_cost']
    target_rate = target_bid / 100.0  # 5.0 → 0.05
    max_ad_rate = PricingEngine.required_ad_rate_for(
        price=live_price, total_cost=cost, safety_margin=safety_margin,
    )
    base = {
        'sku': sku_info['sku'],
        'listing_id': sku_info['listing_id'],
        'live_price': live_price,
        'total_cost': cost,
        'required_ad_rate': max_ad_rate,
        'target_ad_rate': target_rate,
        'safety_margin': safety_margin,
    }
    if max_ad_rate < 0:
        # 现价根本撑不起任何利润缓冲 → 不该开广告 (甚至应再降广告 / 提价)
        return {**base, 'decision': 'skip_unsafe',
                'reason': f'现价 ${live_price:.2f} 即使无广告也无 {safety_margin*100:.0f}% 缓冲'}
    if max_ad_rate + 0.0001 < target_rate:
        return {**base, 'decision': 'skip_low_margin',
                'reason': f'现价撑得起的最大广告费率 {max_ad_rate*100:.2f}% < 目标 {target_bid:.1f}%'}
    return {**base, 'decision': 'restore',
            'reason': f'现价 ${live_price:.2f} 可撑 {max_ad_rate*100:.2f}% 广告 ≥ 目标 {target_bid:.1f}%'}


# ─── 主流程 ──────────────────────────────────────────

def find_default_campaign(ad_svc) -> Optional[str]:
    """找一个 RUNNING campaign 作为默认开广告目标.

    若有多个, 取第一个 (与现有 batch_publish 行为一致).
    """
    try:
        campaigns = ad_svc.fetch_campaigns(status='RUNNING')
        if not campaigns:
            log.error("❌ 无 RUNNING 状态的广告活动, 无法开广告")
            return None
        cid = campaigns[0].get('campaignId', '')
        log.info(f"📌 默认 campaign: {cid} ({campaigns[0].get('campaignName', '')})")
        return cid
    except Exception as e:
        log.error(f"❌ fetch_campaigns 失败: {e}")
        return None


def run_audit(apply_changes: bool, send_email: bool,
              target_bid: float, safety_margin: float,
              limit: Optional[int] = None) -> Dict[str, Any]:
    started = datetime.now()
    log.info("=" * 70)
    log.info(f"🎯 广告恢复审计 ({'APPLY' if apply_changes else 'DRY-RUN'})")
    log.info(f"   target_bid={target_bid:.1f}%, safety_margin={safety_margin*100:.0f}%")
    log.info("=" * 70)

    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_ad_service import EbayAdService

    oauth = EbayOAuthService('PRODUCTION')
    ad_svc = EbayAdService()

    # 1. 加载候选 SKU
    candidates = load_published_skus()
    log.info(f"📦 加载 PUBLISHED + 有成本数据的 SKU: {len(candidates)}")

    if limit:
        candidates = candidates[:limit]
        log.info(f"   (limit={limit})")

    # 2. 一次性拉所有 RUNNING 广告映射 (避免 N 次 API)
    log.info("🔍 加载当前广告映射...")
    all_promoted_listings = set()
    try:
        for c in ad_svc.fetch_campaigns(status='RUNNING'):
            cid = c.get('campaignId', '')
            if not cid:
                continue
            for ad in ad_svc.fetch_campaign_ads(cid):
                lid = str(ad.get('listingId', '') or '')
                if lid:
                    all_promoted_listings.add(lid)
        log.info(f"   当前 RUNNING campaign 中有 {len(all_promoted_listings)} 个 listing 在被推广")
    except Exception as e:
        log.error(f"❌ 加载广告映射失败: {e}")
        return {'error': str(e), 'evaluated': 0}

    # 3. 过滤: 只看当前没在打广告的
    not_promoted = [c for c in candidates if c['listing_id'] not in all_promoted_listings]
    log.info(f"🔎 未在广告中的 SKU: {len(not_promoted)} (这些是恢复审计的范围)")

    # 4. 逐个评估
    decisions = {
        'restore': [],
        'skip_unsafe': [],
        'skip_low_margin': [],
        'skip_not_live': [],
        'no_live_price': [],
    }
    for i, sku_info in enumerate(not_promoted, 1):
        sku = sku_info['sku']
        if i % 25 == 0:
            log.info(f"  ...进度 {i}/{len(not_promoted)}")
        live_state = fetch_live_listing_state(oauth, sku, expected_listing_id=sku_info['listing_id'])
        if live_state.get('state') == 'error':
            decisions['no_live_price'].append({
                'sku': sku,
                'listing_id': sku_info['listing_id'],
                'reason': live_state.get('reason', '无法读取现网 offer'),
            })
            continue
        if live_state.get('state') != 'live':
            decisions['skip_not_live'].append({
                'sku': sku,
                'listing_id': sku_info['listing_id'],
                'reason': live_state.get('reason', 'listing 已结束或本地映射过期'),
                'live_listing_ids': live_state.get('live_listing_ids', []),
            })
            continue
        live_price = float(live_state['live_price'])
        result = evaluate_sku(sku_info, live_price, target_bid, safety_margin)
        decisions[result['decision']].append(result)
        time.sleep(0.05)  # 轻微节流

    # 5. 汇总
    log.info("─" * 70)
    log.info(f"✅ 应恢复广告: {len(decisions['restore'])}")
    log.info(f"⏭️  利润不足跳过: {len(decisions['skip_low_margin'])}")
    log.info(f"⚠️  现价不安全跳过: {len(decisions['skip_unsafe'])}")
    log.info(f"🛑 已结束/错配跳过: {len(decisions['skip_not_live'])}")
    log.info(f"❓ 无现价数据: {len(decisions['no_live_price'])}")

    # 6. 实施
    restored, restore_failed = [], []
    if apply_changes and decisions['restore']:
        target_cid = find_default_campaign(ad_svc)
        if not target_cid:
            log.error("❌ 无可用 campaign, 跳过 apply 阶段")
        else:
            log.info(f"🚀 开始重新开广告 ({len(decisions['restore'])} 个)...")
            for d in decisions['restore']:
                # 用 create_ad_safe: 即使我们的 evaluate 认可, 也再做一次现网价兜底
                # (live_price 可能在 evaluate→create 之间变化)
                r = ad_svc.create_ad_safe(
                    target_cid, d['listing_id'],
                    sku=d['sku'],
                    bid_percentage=target_bid,
                    safety_margin=safety_margin,
                )
                if isinstance(r, dict) and r.get('success'):
                    restored.append(d)
                    log.info(f"  ✅ {d['sku']} → 广告恢复 (现价 ${d['live_price']:.2f}, "
                             f"可撑 {d['required_ad_rate']*100:.2f}%)")
                else:
                    err = r.get('error', '?') if isinstance(r, dict) else str(r)
                    restore_failed.append({**d, 'error': err})
                    log.error(f"  ❌ {d['sku']} → create_ad_safe 失败: {err}")
                time.sleep(0.5)
    elif decisions['restore']:
        log.info("🧪 DRY-RUN — 上面的恢复候选未实际开广告. 加 --apply 真正执行.")

    # 7. 写报告
    report = {
        'started': started.isoformat(),
        'finished': datetime.now().isoformat(),
        'mode': 'apply' if apply_changes else 'dry-run',
        'target_bid_pct': target_bid,
        'safety_margin': safety_margin,
        'totals': {
            'candidates': len(candidates),
            'currently_promoted': len([c for c in candidates if c['listing_id'] in all_promoted_listings]),
            'evaluated_not_promoted': len(not_promoted),
            'should_restore': len(decisions['restore']),
            'skip_low_margin': len(decisions['skip_low_margin']),
            'skip_unsafe': len(decisions['skip_unsafe']),
            'skip_not_live': len(decisions['skip_not_live']),
            'no_live_price': len(decisions['no_live_price']),
            'restored_ok': len(restored),
            'restore_failed': len(restore_failed),
        },
        'restore_candidates': decisions['restore'][:200],
        'skip_unsafe': decisions['skip_unsafe'][:50],
        'skip_not_live': decisions['skip_not_live'][:50],
        'restore_failed': restore_failed,
    }
    report_path = LOG_DIR / f"ad_restore_audit_{started.strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    log.info(f"📄 报告已写: {report_path}")

    if send_email and (restored or restore_failed or decisions['skip_unsafe'] or decisions['skip_not_live']):
        _send_email(report, report_path)

    return report


def _send_email(report: Dict[str, Any], report_path: Path):
    """发送审计邮件 (复用现有邮件服务)."""
    try:
        from src.utils.email_sender import send_email

        t = report['totals']
        subject = f"[广告恢复审计] 恢复 {t['restored_ok']} / 失败 {t['restore_failed']} / 不安全 {t['skip_unsafe']}"
        body = (
            f"模式: {report['mode']}\n"
            f"评估范围 (未在广告中): {t['evaluated_not_promoted']}\n"
            f"应恢复: {t['should_restore']}, 实际恢复成功: {t['restored_ok']}, 失败: {t['restore_failed']}\n"
            f"利润不足跳过: {t['skip_low_margin']}, 现价不安全跳过: {t['skip_unsafe']}\n"
            f"已结束/错配跳过: {t.get('skip_not_live', 0)}\n\n"
            f"详细报告: {report_path}"
        )
        html_body = (
            "<html><body><pre style='font-family:Consolas,monospace;'>"
            f"{escape(body)}"
            "</pre></body></html>"
        )
        ok = send_email(subject, html_body, attachments=[str(report_path)])
        if ok:
            log.info("📧 邮件已发送")
        else:
            log.warning("⚠️ 邮件未成功送达，已保留本地报告")
    except Exception as e:
        log.warning(f"⚠️ 邮件发送失败 (非阻塞): {e}")


def main():
    p = argparse.ArgumentParser(description="广告自动恢复审计")
    p.add_argument('--apply', action='store_true', help='真正调 eBay 开广告 (默认 dry-run)')
    p.add_argument('--email', action='store_true', help='发送审计邮件')
    p.add_argument('--target-bid', type=float, default=5.0, help='目标广告 bid 百分比 (默认 5.0)')
    p.add_argument('--safety', type=float, default=0.05, help='利润安全缓冲 (默认 0.05 即 5%%)')
    p.add_argument('--limit', type=int, default=None, help='仅审计前 N 个 SKU (调试用)')
    args = p.parse_args()

    if not (0 <= args.target_bid <= 25):
        p.error("--target-bid must be in [0, 25]")
    if not (0 <= args.safety <= 0.5):
        p.error("--safety must be in [0, 0.5]")

    report = run_audit(
        apply_changes=args.apply,
        send_email=args.email,
        target_bid=args.target_bid,
        safety_margin=args.safety,
        limit=args.limit,
    )
    if 'error' in report:
        sys.exit(2)
    # 单条 listing 的恢复失败写入报告，但不让调度器把整次审计判为失败。
    sys.exit(0)


if __name__ == '__main__':
    main()
