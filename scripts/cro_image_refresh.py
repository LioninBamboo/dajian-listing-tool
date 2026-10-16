"""CRO image_refresh 执行器 — 把 cro_action_queue 里 image_refresh 动作真改图.

策略 (保守):
  1. 拉队列里 status=pending 且 action=image_refresh 的 SKU
  2. 对每个 SKU:
     a. 读本地 products.image_urls (JSON 串) → 获得候选图列表
     b. GET eBay Inventory item 当前 imageUrls
     c. 若本地图数 > 1 且与远端首图不同 (或远端只有 1 张)
        → 用本地完整列表替换 (保留远端 title/description/aspects/availability)
     d. 二次 GET 验证生效后才 mark_done
  3. 改完写 logs/cro_image_refresh_<date>.json + 可选邮件汇总

不绕过现有规则: 复用 RealEbayClient.create_or_replace_inventory_item, eBay 自身校验图片.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.services.cro_action_queue import load_pending, mark_done  # noqa: E402

logger = logging.getLogger(__name__)


def _parse_image_urls(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw)
        except Exception:
            items = [raw] if isinstance(raw, str) else []
    out = []
    seen = set()
    for u in items:
        if not isinstance(u, str):
            continue
        s = u.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _load_local_product(sku: str) -> Dict[str, Any]:
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct

    with SessionLocal() as s:
        p = s.query(CollectedProduct).filter(CollectedProduct.sku == sku).first()
        if not p:
            return {'images': [], 'title': '', 'description': ''}
        return {
            'images': _parse_image_urls(p.images),
            'title': p.title or '',
            'description': p.description or '',
        }


def _load_local_images(sku: str) -> List[str]:
    return _load_local_product(sku)['images']


def _should_refresh(local: List[str], live_urls: List[str]) -> Optional[str]:
    """返回 None 表示无需刷新; 否则返回原因."""
    if len(local) < 2:
        return None  # 本地都没多图, 没的换
    if not live_urls:
        return 'live has no images'
    if len(live_urls) <= 1 and len(local) >= 2:
        return f'live only {len(live_urls)} img vs local {len(local)}'
    if local[0] != live_urls[0]:
        return 'local lead image differs from live'
    return None


def _refresh_one(client, sku: str, local: Dict[str, Any]) -> Dict[str, Any]:
    item = client.get_inventory_item(sku)
    if not item:
        return {'sku': sku, 'status': 'skipped', 'reason': 'no inventory item'}
    product = (item.get('product') or {})
    local_images = local.get('images') or []
    live_urls = product.get('imageUrls') or []
    reason = _should_refresh(local_images, live_urls)
    if not reason:
        return {'sku': sku, 'status': 'skipped',
                'reason': 'live already healthy', 'live_n': len(live_urls)}

    title = (product.get('title') or local.get('title') or sku).strip()
    description = (product.get('description') or local.get('description') or title).strip()
    description = description[:4000] or title[:4000]
    payload = {
        'title': title,
        'description': description,
        'image_urls': local_images,
        'condition': item.get('condition', 'NEW'),
        'quantity': (((item.get('availability') or {}).get(
            'shipToLocationAvailability') or {}).get('quantity', 1)),
        'aspects': product.get('aspects') or {},
    }
    try:
        client.create_or_replace_inventory_item(sku, payload)
    except Exception as e:
        return {'sku': sku, 'status': 'failed', 'reason': f'PUT error: {e}'}

    # 二次验证
    item2 = client.get_inventory_item(sku) or {}
    live2 = ((item2.get('product') or {}).get('imageUrls')) or []
    expected_count = min(len(local_images), 24)
    if not live2:
        return {'sku': sku, 'status': 'failed',
                'reason': 'verification mismatch',
                'expected_count': expected_count, 'live_n': 0}
    if len(live2) < max(1, expected_count - 1):
        return {'sku': sku, 'status': 'failed',
                'reason': 'verification image count mismatch',
                'expected_count': expected_count, 'live_n': len(live2)}
    return {'sku': sku, 'status': 'done', 'reason': reason,
            'live_n': len(live2), 'local_n': len(local_images)}


def _default_ebay_client():
    from dotenv import load_dotenv
    from src.clients.real_ebay_client import create_real_ebay_client

    load_dotenv(PROJECT_ROOT / ".env")
    return create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))


def run(apply_changes: bool, limit: int) -> Dict[str, Any]:
    pending = load_pending(action_type='image_refresh')[:limit]
    rep: Dict[str, Any] = {
        'started_at': datetime.now().isoformat(),
        'apply': apply_changes,
        'pending_total': len(pending),
        'rows': [],
        'done': [],
        'failed': [],
        'skipped': [],
    }
    if not pending:
        return rep

    client = _default_ebay_client() if apply_changes else None

    done_skus: List[str] = []
    skipped_skus: List[str] = []
    marked_done_count = 0
    for action in pending:
        sku = action.get('sku')
        if not sku:
            continue
        local = _load_local_product(sku)
        local_images = local.get('images') or []
        if not local_images:
            row = {'sku': sku, 'status': 'skipped', 'reason': 'no local images'}
        elif not apply_changes:
            row = {'sku': sku, 'status': 'dry_run',
                   'reason': 'would apply', 'local_n': len(local_images)}
        else:
            try:
                row = _refresh_one(client, sku, local)
            except Exception as e:
                row = {'sku': sku, 'status': 'failed', 'reason': f'unhandled: {e}'}
        rep['rows'].append(row)
        bucket = row['status'] if row['status'] in ('done', 'failed', 'skipped') else 'skipped'
        rep[bucket].append(sku)
        if row['status'] == 'done':
            done_skus.append(sku)
            if apply_changes:
                marked_done_count += mark_done([sku], action='image_refresh')
        elif row['status'] == 'skipped' and apply_changes:
            skipped_skus.append(sku)

    if done_skus and apply_changes:
        rep['marked_done'] = marked_done_count
    if skipped_skus and apply_changes:
        # 终态跳过 (本地无图 / 无 inventory) 是结构性无操作, 从 pending 出队,
        # 否则每天被重新拉出空转. 与 fill_specifics 同处理.
        rep['marked_skipped'] = mark_done(
            skipped_skus, action='image_refresh', result='skipped')

    rep['finished_at'] = datetime.now().isoformat()
    return rep


def main():
    p = argparse.ArgumentParser(description='CRO image_refresh executor')
    p.add_argument('--apply', action='store_true', help='Actually PUT to eBay')
    p.add_argument('--limit', type=int, default=20, help='Max SKUs per run (default 20)')
    p.add_argument('--out', help='Write json report to path')
    p.add_argument('--email', action='store_true', help='Email summary on done/failed')
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
                else PROJECT_ROOT / 'logs'
                / f"cro_image_refresh_{datetime.now():%Y%m%d_%H%M%S}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    print(f'Report → {out_path}')

    if args.email and (rep['done'] or rep['failed']):
        try:
            from src.utils.email_sender import send_email
            html = f'''
            <h2>🖼️ CRO 图片刷新汇总</h2>
            <p>队列待处理: {rep['pending_total']} · 改成功: <b>{len(rep['done'])}</b> ·
               失败: {len(rep['failed'])} · 跳过: {len(rep['skipped'])}</p>
            <pre style="font-size:12px;">{json.dumps(rep['rows'][:50], ensure_ascii=False, indent=2)}</pre>
            '''
            send_email(
                f"🖼️ CRO 图片刷新 - 成功{len(rep['done'])} 失败{len(rep['failed'])}",
                html,
            )
        except Exception as e:
            print(f'Email failed: {e}')


if __name__ == '__main__':
    main()
