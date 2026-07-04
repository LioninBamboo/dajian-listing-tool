"""CRO send_offer 执行器 — 给 watchers/加购买家发保本限时 offer.

针对 low_cvr (有点击不下单) 的 SKU, 用 eBay Negotiation API 的
Seller-Initiated Offer 直接刺激下单. 这是漏斗最后一级的转化杠杆.

保本设计 (不亏本的四道闸):
  1. 地板价: cro_offer_pricing 用 PricingEngine 同一套费率常量推导
     最低净利率 5% 的地板价; offer 价 < 地板 → 抬到地板;
     地板 ≥ 现价 → 无让利空间, 终态 skip.
  2. 折扣封顶: 单次 offer 最多 10% (默认 5%).
  3. 频控: 同一 SKU 30 天内只发一次 (读本执行器历史日志);
     eBay 自身也限制同一买家对同一 listing 的重复 offer.
  4. A/B 对照: send_offer 在 AB_ENABLED_ACTIONS 里, 20% 队列行为
     control 不执行, cro_effect_audit 验证增量.

流程:
  1. 拉队列 status=pending 且 action=send_offer 的 SKU
  2. GET find_eligible_items → 只对有 interested buyers 的 listing 发
  3. load_sku_economics + compute_offer → 保本校验
  4. POST send_offer_to_interested_buyers → mark_done
调度: 每日 10:35 (在 promote 之后, sentinel 之前).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.cro_action_queue import load_pending, mark_done  # noqa: E402
from src.services.cro_sku_economics import load_sku_economics  # noqa: E402
from src.services.cro_offer_pricing import compute_offer  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'
DEFAULT_LOGS_DIR = PROJECT_ROOT / 'logs'
OFFER_COOLDOWN_DAYS = 30
OFFER_MESSAGE = ("Thanks for your interest! Here's a limited-time offer "
                 "just for you — free shipping included.")
_LOG_NAME_RE = re.compile(r"cro_send_offer_(\d{8})_\d{6}\.json$")


def recently_offered_skus(days: int = OFFER_COOLDOWN_DAYS,
                          logs_dir: Optional[Path] = None) -> Set[str]:
    """近 N 天已发过 offer 的 SKU — 防连环让利训练出等折扣买家."""
    d = Path(logs_dir) if logs_dir else DEFAULT_LOGS_DIR
    if not d.is_dir():
        return set()
    cutoff = datetime.now().date() - timedelta(days=days)
    out: Set[str] = set()
    for p in d.glob("cro_send_offer_*.json"):
        m = _LOG_NAME_RE.search(p.name)
        if not m:
            continue
        try:
            if datetime.strptime(m.group(1), "%Y%m%d").date() < cutoff:
                continue
            rep = json.loads(p.read_text(encoding='utf-8'))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        for row in rep.get('rows', []) or []:
            if isinstance(row, dict) and row.get('status') == 'done' and row.get('sku'):
                out.add(str(row['sku']))
    return out


def _lookup_listing_id(sku: str, db_path: Optional[Path] = None) -> Optional[str]:
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return None
    try:
        with sqlite3.connect(str(db)) as c:
            r = c.execute(
                "SELECT listing_id FROM collected_products WHERE sku=?",
                (sku,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(r[0]) if r and r[0] else None


# ── Negotiation API ─────────────────────────────────────────

def _build_oauth():
    from dotenv import load_dotenv
    from src.services.ebay_auth import EbayOAuthService
    load_dotenv(PROJECT_ROOT / '.env')
    oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
    if not oauth.is_authorized():
        raise RuntimeError('eBay OAuth is not authorized')
    return oauth


def _headers(oauth) -> Dict[str, str]:
    return {
        'Authorization': f'Bearer {oauth.get_valid_token()}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
    }


def fetch_eligible_listing_ids(oauth, limit: int = 200) -> Set[str]:
    """GET find_eligible_items — 有 interested buyers 的 listing 集合."""
    import requests
    url = f"{oauth.api_base}/sell/negotiation/v1/find_eligible_items"
    out: Set[str] = set()
    offset = 0
    while True:
        resp = requests.get(url, headers=_headers(oauth),
                            params={'limit': min(limit, 200), 'offset': offset},
                            timeout=40)
        if resp.status_code != 200:
            logger.warning('find_eligible_items %s: %s',
                           resp.status_code, resp.text[:200])
            break
        data = resp.json() or {}
        for item in data.get('eligibleItems', []) or []:
            lid = str(item.get('listingId') or '').strip()
            if lid:
                out.add(lid)
        total = int(data.get('total') or 0)
        offset += len(data.get('eligibleItems') or [])
        if offset >= total or not data.get('eligibleItems'):
            break
    return out


def send_offer_live(oauth, listing_id: str, offer_price: float,
                    message: str = OFFER_MESSAGE) -> Dict[str, Any]:
    """POST send_offer_to_interested_buyers. 不允许还价 (保本闸)."""
    import requests
    url = (f"{oauth.api_base}"
           "/sell/negotiation/v1/send_offer_to_interested_buyers")
    body = {
        'allowCounterOffer': False,
        'message': message,
        'offeredItems': [{
            'listingId': str(listing_id),
            'quantity': '1',
            'price': {'currency': 'USD', 'value': f'{offer_price:.2f}'},
        }],
    }
    resp = requests.post(url, headers=_headers(oauth), json=body, timeout=60)
    if resp.status_code in (200, 201):
        return {'ok': True, 'reason': 'sent'}
    return {'ok': False,
            'reason': f'send_offer_{resp.status_code}: {resp.text[:200]}'}


# ── 主流程 ───────────────────────────────────────────────────

def run(apply_changes: bool, limit: int,
        db_path: Optional[Path] = None,
        logs_dir: Optional[Path] = None,
        oauth=None) -> Dict[str, Any]:
    pending = load_pending(action_type='send_offer')[:limit]
    rep: Dict[str, Any] = {
        'started_at': datetime.now().isoformat(),
        'apply': apply_changes,
        'pending_total': len(pending),
        'rows': [], 'done': [], 'failed': [], 'skipped': [],
    }
    if not pending:
        return rep

    cooldown = recently_offered_skus(logs_dir=logs_dir)
    if apply_changes and oauth is None:
        oauth = _build_oauth()
    eligible = fetch_eligible_listing_ids(oauth) if apply_changes else set()

    done_skus: List[str] = []
    terminal_skipped: List[str] = []
    for action in pending:
        sku = str(action.get('sku') or '').strip()
        if not sku:
            continue
        detail = action.get('detail') or {}
        try:
            discount = float(detail.get('suggested_discount_pct') or 5.0)
        except (TypeError, ValueError):
            discount = 5.0

        if sku in cooldown:
            row = {'sku': sku, 'status': 'skipped',
                   'reason': f'offer cooldown ({OFFER_COOLDOWN_DAYS}d)'}
            rep['rows'].append(row)
            rep['skipped'].append(sku)
            continue

        eco = load_sku_economics(sku, db_path=db_path)
        offer = compute_offer(eco['price'], eco['cost'],
                              discount_pct=discount) if eco else {
            'safe': False, 'reason': 'missing economics (price/cost)'}
        if not offer['safe']:
            # 保本闸拦下 — 终态 skip, 24h 内不会被 daily runner 重新入队
            row = {'sku': sku, 'status': 'skipped',
                   'reason': f"offer guard: {offer['reason']}",
                   'terminal': True}
            rep['rows'].append(row)
            rep['skipped'].append(sku)
            terminal_skipped.append(sku)
            continue

        listing_id = str(action.get('listing_id') or '').strip() \
            or _lookup_listing_id(sku, db_path=db_path)
        if not listing_id:
            row = {'sku': sku, 'status': 'skipped',
                   'reason': 'no listing_id', 'terminal': True}
            rep['rows'].append(row)
            rep['skipped'].append(sku)
            terminal_skipped.append(sku)
            continue

        if not apply_changes:
            row = {'sku': sku, 'status': 'dry_run',
                   'listing_id': listing_id,
                   'offer_price': offer['offer_price'],
                   'floor': offer['floor'],
                   'discount_pct': offer['discount_pct']}
            rep['rows'].append(row)
            rep['skipped'].append(sku)
            continue

        if listing_id not in eligible:
            # 无 interested buyers — 非终态, 明天买家出现后可再试
            row = {'sku': sku, 'status': 'skipped',
                   'reason': 'no interested buyers yet',
                   'listing_id': listing_id}
            rep['rows'].append(row)
            rep['skipped'].append(sku)
            continue

        try:
            res = send_offer_live(oauth, listing_id, offer['offer_price'])
        except Exception as e:
            res = {'ok': False, 'reason': f'unhandled: {e}'}
        row = {'sku': sku, 'listing_id': listing_id,
               'offer_price': offer['offer_price'],
               'floor': offer['floor'],
               'discount_pct': offer['discount_pct']}
        if res['ok']:
            row['status'] = 'done'
            rep['done'].append(sku)
            done_skus.append(sku)
        else:
            row['status'] = 'failed'
            row['reason'] = res['reason']
            rep['failed'].append(sku)
        rep['rows'].append(row)
        time.sleep(1.0)

    if apply_changes:
        if done_skus:
            rep['marked_done'] = mark_done(done_skus, action='send_offer')
        if terminal_skipped:
            rep['marked_skipped'] = mark_done(
                terminal_skipped, action='send_offer', result='skipped')

    rep['finished_at'] = datetime.now().isoformat()
    return rep


def main():
    p = argparse.ArgumentParser(description='CRO send_offer executor')
    p.add_argument('--apply', action='store_true',
                   help='Actually send offers via Negotiation API')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--out')
    p.add_argument('--email', action='store_true')
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    rep = run(apply_changes=args.apply, limit=args.limit)
    print(json.dumps({
        'pending_total': rep['pending_total'],
        'done': len(rep['done']),
        'failed': len(rep['failed']),
        'skipped': len(rep['skipped']),
        'apply': rep['apply'],
    }, indent=2))

    out_path = (Path(args.out) if args.out
                else DEFAULT_LOGS_DIR
                / f"cro_send_offer_{datetime.now():%Y%m%d_%H%M%S}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    print(f'Report → {out_path}')

    if args.email and (rep['done'] or rep['failed']):
        try:
            from src.utils.email_sender import send_email
            html = f'''
            <h2>💌 CRO Seller-Initiated Offer 汇总</h2>
            <p>队列: {rep['pending_total']} · 已发: <b>{len(rep['done'])}</b> ·
               失败: {len(rep['failed'])} · 跳过: {len(rep['skipped'])}</p>
            <pre style="font-size:12px;">{json.dumps(rep['rows'][:50], ensure_ascii=False, indent=2)}</pre>
            '''
            send_email(
                f"💌 CRO Offer - 已发{len(rep['done'])} 失败{len(rep['failed'])}",
                html,
            )
        except Exception as e:
            print(f'Email failed: {e}')


if __name__ == '__main__':
    main()
