"""S23 — CRO promote 执行器: 把 P1/P2 promote 动作落地为提升 bid_percentage.

策略 (保守):
  1. 拉队列里 status=pending 且 action=promote 的 SKU
  2. 对每个 SKU:
     a. 通过 products.ebay_item_id 找到 listing_id
     b. find_ad_for_listing 检查是否已被推广
        - 已推广: 把 bid_percentage 增加 detail.suggested_bid_pct (默认 5)
          - 未推广: 选一个 RUNNING campaign, 走 create_ad_safe 开广告
      c. update_ad_bid/create_ad_safe 成功后 mark_done

不绕过现有规则: 复用 EbayAdService.update_ad_bid / create_ad_safe.
调度: 每日 10:25 (在 CRO 队列消费窗口内).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.cro_action_queue import load_pending, mark_done  # noqa: E402
from src.services.cro_email_digest import render_promote_email  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_BID_INCREMENT = 5.0  # %
MAX_BID_PCT = 25.0           # 安全上限


def _lookup_listing_id(sku: str) -> Optional[str]:
    try:
        from src.db.database import SessionLocal
        from src.db.models import Product
    except Exception:
        SessionLocal = None
        Product = None
    if SessionLocal and Product:
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.sku == sku).first()
            if p:
                listing_id = getattr(p, 'ebay_item_id', None)
                if listing_id:
                    return str(listing_id)
    try:
        from src.db.collection_db import SessionLocal as CollectionSessionLocal
        from src.db.collection_models import CollectedProduct
        with CollectionSessionLocal() as s:
            p = s.query(CollectedProduct).filter(CollectedProduct.sku == sku).first()
            if not p:
                return None
            listing_id = getattr(p, 'ebay_item_id', None) or getattr(p, 'listing_id', None)
            return str(listing_id) if listing_id else None
    except Exception:
        return None


def _select_campaign_id(ad_service, preferred_campaign_id: Optional[str] = None) -> Optional[str]:
    if preferred_campaign_id:
        return str(preferred_campaign_id)
    try:
        campaigns = ad_service.fetch_campaigns(status='RUNNING') or []
    except Exception:
        return None
    for campaign in campaigns:
        campaign_id = campaign.get('campaignId') or campaign.get('id')
        if campaign_id:
            return str(campaign_id)
    return None


def _promote_one(ad_service, sku: str, suggested_bid_pct: float,
                 *, listing_id: Optional[str] = None,
                 campaign_id: Optional[str] = None,
                 create_if_missing: bool = False) -> Dict[str, Any]:
    listing_id = str(listing_id or _lookup_listing_id(sku) or '').strip()
    if not listing_id:
        return {'sku': sku, 'status': 'skipped',
                'reason': 'no ebay_item_id in DB'}
    ad = ad_service.find_ad_for_listing(listing_id)
    if not ad:
        if not create_if_missing:
            return {'sku': sku, 'status': 'skipped',
                    'reason': 'not promoted (auto create disabled)',
                    'listing_id': listing_id}
        target_campaign = _select_campaign_id(ad_service, campaign_id)
        if not target_campaign:
            return {'sku': sku, 'status': 'skipped',
                    'reason': 'not promoted and no running campaign',
                    'listing_id': listing_id}
        res = ad_service.create_ad_safe(
            target_campaign,
            listing_id,
            sku=sku,
            bid_percentage=suggested_bid_pct,
        )
        if not res or not res.get('success'):
            guard_reason = (res or {}).get('reason') or ''
            err = str((res or {}).get('error') or '')
            # listing 已结束 (35037) 也是终态 — 死链每天重试没有意义
            terminal = (guard_reason in {'unsafe', 'blacklisted'}
                        or '35037' in err or 'has ended' in err)
            return {'sku': sku, 'status': 'skipped' if terminal else 'failed',
                'reason': err or guard_reason or 'create_ad_safe failed',
                    'listing_id': listing_id,
                'campaign_id': target_campaign,
                'guard_reason': guard_reason,
                'terminal': terminal}
        return {'sku': sku, 'status': 'done',
                'listing_id': listing_id,
                'campaign_id': target_campaign,
                'new_bid': suggested_bid_pct,
                'created_ad': True}
    cur_bid = float(ad.get('bid_percentage') or 0.0)
    # S36: \u5229\u6da6\u611f\u77e5\u52a8\u6001\u4e0a\u9650; \u627e\u4e0d\u5230\u6210\u672c\u4ef7 \u2192 fallback \u5230 HARD_FLOOR_PCT
    try:
        from src.services.cro_margin_aware_bid import bid_cap_for_sku
        dyn_cap = bid_cap_for_sku(sku)
    except Exception:
        dyn_cap = MAX_BID_PCT
    effective_cap = min(MAX_BID_PCT, dyn_cap)
    # eBay Marketing API \u53ea\u63a5\u53d7 1 \u4f4d\u5c0f\u6570\u7684 bidPercentage (35007);
    # \u5411\u4e0b\u53d6\u6574\u5230 0.1, \u4e0d\u8d8a\u5229\u6da6 cap. margin cap \u4fee\u590d\u540e\u4ea7\u51fa 7.05/13.93
    # \u8fd9\u7c7b\u4e24\u4f4d\u5c0f\u6570, 2026-07-12 \u66fe\u5bfc\u81f4\u5168\u90e8 update 400 \u7a7a\u8f6c.
    import math
    new_bid = math.floor(
        min(cur_bid + suggested_bid_pct, effective_cap) * 10) / 10
    if new_bid <= cur_bid:
        return {'sku': sku, 'status': 'skipped',
                'reason': f'already at cap ({cur_bid}% + {suggested_bid_pct}% > {effective_cap}%)',
                'cur_bid': cur_bid, 'dyn_cap': dyn_cap,
                'terminal': True}
    res = ad_service.update_ad_bid(ad['campaign_id'], listing_id, new_bid)
    if not res or not res.get('success'):
        err = str((res or {}).get('error') or 'update_ad_bid failed')
        if '35037' in err or 'has ended' in err:
            # listing \u5df2\u7ed3\u675f \u2014 \u7ec8\u6001 skip, \u522b\u518d\u6bcf\u5929\u91cd\u8bd5\u6b7b\u94fe
            return {'sku': sku, 'status': 'skipped',
                    'reason': f'listing ended: {err[:120]}',
                    'listing_id': listing_id, 'terminal': True}
        return {'sku': sku, 'status': 'failed',
                'reason': err,
                'cur_bid': cur_bid, 'target_bid': new_bid}
    return {'sku': sku, 'status': 'done',
            'listing_id': listing_id,
            'cur_bid': cur_bid, 'new_bid': new_bid,
            'increment': suggested_bid_pct}


def run(apply_changes: bool, limit: int,
        auto_create_missing_ads: bool = True,
        campaign_id: Optional[str] = None) -> Dict[str, Any]:
    pending = load_pending(action_type='promote')[:limit]
    rep: Dict[str, Any] = {
        'started_at': datetime.now().isoformat(),
        'apply': apply_changes,
        'auto_create_missing_ads': auto_create_missing_ads,
        'pending_total': len(pending),
        'rows': [], 'done': [], 'failed': [], 'skipped': [],
    }
    if not pending:
        return rep

    if apply_changes:
        from src.services.ebay_ad_service import EbayAdService
        ad_service = EbayAdService()
    else:
        ad_service = None

    done_skus: List[str] = []
    terminal_skipped_skus: List[str] = []
    for action in pending:
        sku = action.get('sku')
        if not sku:
            continue
        detail = action.get('detail') or {}
        try:
            inc = float(detail.get('suggested_bid_pct') or DEFAULT_BID_INCREMENT)
        except (TypeError, ValueError):
            inc = DEFAULT_BID_INCREMENT
        if not apply_changes:
            row = {'sku': sku, 'status': 'dry_run',
                   'reason': 'would apply', 'increment': inc,
                   'auto_create_missing_ads': auto_create_missing_ads}
        else:
            try:
                row = _promote_one(
                    ad_service,
                    sku,
                    inc,
                    listing_id=action.get('listing_id'),
                    campaign_id=campaign_id,
                    create_if_missing=auto_create_missing_ads,
                )
            except Exception as e:
                row = {'sku': sku, 'status': 'failed', 'reason': f'unhandled: {e}'}
        rep['rows'].append(row)
        bucket = row['status'] if row['status'] in ('done', 'failed', 'skipped') else 'skipped'
        rep[bucket].append(sku)
        if row['status'] == 'done':
            done_skus.append(sku)
        elif row['status'] == 'skipped' and row.get('terminal'):
            terminal_skipped_skus.append(sku)

    if done_skus and apply_changes:
        rep['marked_done'] = mark_done(done_skus, action='promote')
    if terminal_skipped_skus and apply_changes:
        rep['marked_skipped'] = mark_done(
            terminal_skipped_skus,
            action='promote',
            result='skipped',
        )

    rep['finished_at'] = datetime.now().isoformat()
    return rep


def main():
    p = argparse.ArgumentParser(description='CRO promote executor')
    p.add_argument('--apply', action='store_true', help='Actually PUT to eBay')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--no-auto-create', action='store_true',
                   help='Only raise bids for existing ads; do not create missing ads')
    p.add_argument('--campaign-id', help='Campaign to use for newly promoted listings')
    p.add_argument('--out')
    p.add_argument('--email', action='store_true')
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    rep = run(
        apply_changes=args.apply,
        limit=args.limit,
        auto_create_missing_ads=not args.no_auto_create,
        campaign_id=args.campaign_id,
    )
    print(json.dumps({
        'pending_total': rep['pending_total'],
        'done': len(rep['done']),
        'failed': len(rep['failed']),
        'skipped': len(rep['skipped']),
        'apply': rep['apply'],
    }, indent=2))

    out_path = (Path(args.out) if args.out
                else PROJECT_ROOT / 'logs'
                / f"cro_promote_{datetime.now():%Y%m%d_%H%M%S}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    print(f'Report → {out_path}')

    if args.email and rep.get('pending_total'):
        try:
            from src.utils.email_sender import send_email
            html = render_promote_email(rep)
            send_email(
                f"📈 CRO Promote · 提价 {len(rep['done'])} / 失败 {len(rep['failed'])}",
                html,
            )
        except Exception as e:
            logger.warning(f'email failed: {e}')


if __name__ == '__main__':
    main()
