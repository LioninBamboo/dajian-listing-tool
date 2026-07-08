"""CRO fill_specifics 执行器 — 给 low_cvr 的 listing 补全 item_specifics.

策略 (保守):
  1. 拉队列里 status=pending 且 action=fill_specifics 的 SKU
  2. 对每个 SKU:
     a. GET inventory item → 现有 aspects + title + description
     b. GET offer → 现 categoryId
     c. EbayCategoryMatcher.get_category_and_aspects(title, aspects, desc) → completed_aspects (含品类必填 + 推断值)
     d. 仅"补"键 (不覆盖已有非空值), 跳过 placeholder / 长度异常的 AI 值
     e. PUT inventory_item → 二次 GET 验证 keys 比之前多 → mark_done
不绕过 EbayPublisher / 校验, 只用 matcher 的输出.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.services.cro_action_queue import load_pending, mark_done  # noqa: E402

logger = logging.getLogger(__name__)

# 不要从 AI 自动补的字段 — 必须有真实源数据
PROTECTED_KEYS = {
    "Item Length", "Item Width", "Item Height",
    "Item Weight", "Package Weight",
}


def _is_meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_is_meaningful(v) for v in value)
    s = str(value).strip()
    if not s:
        return False
    low = s.lower()
    if low in {"n/a", "na", "none", "null", "tbd", "unknown", "0", "0.0"}:
        return False
    return True


def _merge_missing(current: Dict[str, List[str]],
                   completed: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """把 completed 里 current 没有 (或当前值无意义) 的键补进去, 跳过 PROTECTED_KEYS."""
    out = {k: list(v) if isinstance(v, list) else [str(v)]
           for k, v in (current or {}).items()}
    for k, v in (completed or {}).items():
        if k in PROTECTED_KEYS:
            continue
        if not _is_meaningful(v):
            continue
        cur = out.get(k)
        if _is_meaningful(cur):
            continue
        out[k] = list(v) if isinstance(v, list) else [str(v)]
    return out


def _missing_keys(current: Dict[str, List[str]],
                  completed: Dict[str, List[str]]) -> Set[str]:
    miss: Set[str] = set()
    for k, v in (completed or {}).items():
        if k in PROTECTED_KEYS:
            continue
        if not _is_meaningful(v):
            continue
        if not _is_meaningful((current or {}).get(k)):
            miss.add(k)
    return miss


def _load_local_product(sku: str) -> Dict[str, str]:
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct

    with SessionLocal() as s:
        p = s.query(CollectedProduct).filter(CollectedProduct.sku == sku).first()
        if not p:
            return {'title': '', 'description': ''}
        return {'title': p.title or '', 'description': p.description or ''}


def _fill_one(client, matcher, sku: str) -> Dict[str, Any]:
    item = client.get_inventory_item(sku)
    if not item:
        return {'sku': sku, 'status': 'skipped', 'reason': 'no inventory item'}
    product = (item.get('product') or {})
    local = _load_local_product(sku)
    title = (product.get('title') or local.get('title') or sku).strip()
    description = (product.get('description') or local.get('description') or title).strip()
    description = description[:4000] or title[:4000]
    cur_aspects = product.get('aspects') or {}

    if not title:
        return {'sku': sku, 'status': 'skipped', 'reason': 'no title'}

    try:
        _cat_id, _cat_name, completed = matcher.get_category_and_aspects(
            title, cur_aspects, description,
        )
    except Exception as e:
        return {'sku': sku, 'status': 'failed',
                'reason': f'matcher error: {e}'}

    missing = _missing_keys(cur_aspects, completed)
    if not missing:
        return {'sku': sku, 'status': 'skipped',
                'reason': 'no missing aspect keys'}

    new_aspects = _merge_missing(cur_aspects, completed)

    payload = {
        'title': title,
        'description': description,
        'image_urls': product.get('imageUrls') or [],
        'condition': item.get('condition', 'NEW'),
        'quantity': (((item.get('availability') or {}).get(
            'shipToLocationAvailability') or {}).get('quantity', 1)),
        'aspects': new_aspects,
    }
    try:
        client.create_or_replace_inventory_item(sku, payload)
    except Exception as e:
        return {'sku': sku, 'status': 'failed', 'reason': f'PUT error: {e}'}

    item2 = client.get_inventory_item(sku) or {}
    live_aspects2 = (item2.get('product') or {}).get('aspects') or {}
    added = [k for k in missing if _is_meaningful(live_aspects2.get(k))]
    if not added:
        return {'sku': sku, 'status': 'failed',
                'reason': 'verification: no new aspects took effect',
                'attempted': sorted(missing)}
    return {'sku': sku, 'status': 'done',
            'added_keys': added,
            'count': len(added)}


def _default_ebay_client():
    from dotenv import load_dotenv
    from src.clients.real_ebay_client import create_real_ebay_client

    load_dotenv(PROJECT_ROOT / ".env")
    return create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))


def _default_category_matcher():
    from dotenv import load_dotenv
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    load_dotenv(PROJECT_ROOT / ".env")
    return EbayCategoryMatcher(EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")))


def run(apply_changes: bool, limit: int) -> Dict[str, Any]:
    pending = load_pending(action_type='fill_specifics')[:limit]
    rep: Dict[str, Any] = {
        'started_at': datetime.now().isoformat(),
        'apply': apply_changes,
        'pending_total': len(pending),
        'rows': [], 'done': [], 'failed': [], 'skipped': [],
    }
    if not pending:
        return rep

    client = _default_ebay_client() if apply_changes else None
    matcher = _default_category_matcher() if apply_changes else None

    done_skus: List[str] = []
    skipped_skus: List[str] = []
    for action in pending:
        sku = action.get('sku')
        if not sku:
            continue
        if not apply_changes:
            row = {'sku': sku, 'status': 'dry_run', 'reason': 'would apply'}
        else:
            try:
                row = _fill_one(client, matcher, sku)
            except Exception as e:
                row = {'sku': sku, 'status': 'failed',
                       'reason': f'unhandled: {e}'}
        rep['rows'].append(row)
        bucket = row['status'] if row['status'] in (
            'done', 'failed', 'skipped') else 'skipped'
        rep[bucket].append(sku)
        if row['status'] == 'done':
            done_skus.append(sku)
        elif row['status'] == 'skipped':
            skipped_skus.append(sku)

    if apply_changes:
        if done_skus:
            rep['marked_done'] = mark_done(done_skus, action='fill_specifics')
        if skipped_skus:
            # 终态跳过 (specifics 已完整 / 无 inventory / 无标题) 是结构性无操作,
            # 隔天不会变. 从 pending 出队, 否则这些 SKU 永久滞留队列, 每天被
            # 重新拉出、白烧一次 get_inventory_item + matcher 调用 (空转).
            rep['marked_skipped'] = mark_done(
                skipped_skus, action='fill_specifics', result='skipped')

    rep['finished_at'] = datetime.now().isoformat()
    return rep


def main():
    p = argparse.ArgumentParser(description='CRO fill_specifics executor')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--out')
    p.add_argument('--email', action='store_true')
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    rep = run(apply_changes=args.apply, limit=args.limit)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in rep.items() if k != 'rows'}, indent=2))
    out_path = (Path(args.out) if args.out
                else PROJECT_ROOT / 'logs'
                / f"cro_fill_specifics_{datetime.now():%Y%m%d_%H%M%S}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    print(f'Report → {out_path}')

    if args.email and (rep['done'] or rep['failed']):
        try:
            from src.utils.email_sender import send_email
            html = f'''
            <h2>📋 CRO 补 specifics 汇总</h2>
            <p>队列: {rep['pending_total']} · 成功: <b>{len(rep['done'])}</b> ·
               失败: {len(rep['failed'])} · 跳过: {len(rep['skipped'])}</p>
            <pre style="font-size:12px;">{json.dumps(rep['rows'][:50], ensure_ascii=False, indent=2)}</pre>
            '''
            send_email(
                f"📋 CRO 补 specifics - 成功{len(rep['done'])} 失败{len(rep['failed'])}",
                html,
            )
        except Exception as e:
            print(f'Email failed: {e}')


if __name__ == '__main__':
    main()
