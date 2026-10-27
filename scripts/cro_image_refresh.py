"""CRO image_refresh 执行器 — 把 cro_action_queue 里 image_refresh 动作真改图.

策略 (保守):
  1. 拉队列里 status=pending 且 action=image_refresh 的 SKU
  2. 对每个 SKU:
     a. 读本地 products.image_urls (JSON 串) → 获得候选图列表
     b. GET eBay Inventory item 当前 imageUrls
     c. 若本地图数 > 1 且与远端首图不同 (或远端只有 1 张)
        → 以 live GET 的深拷贝作为 PUT 体, 只替换 product.imageUrls
        live quantity 缺失 / 无法解析 / <= 0 时跳过, 绝不默认成 1, 也不发 PUT
     d. 二次 GET, 读回 imageUrls 张数必须等于写出张数, 少一张也失败并留在队列
  3. 改完写 logs/cro_image_refresh_<date>.json + 可选邮件汇总

PUT 体是完整 inventory item (createOrReplace 会整单替换). 不走
create_or_replace_inventory_item 的局部字段重建, 避免 quantity 被默认成 1
并冲掉 packageWeightAndSize / locale / identifiers.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


# GET 回包里不能原样写回的字段. locale / availability 要保留.
# packageWeightAndSize 保留, 但 weight.value <= 0 不能写回 (eBay 25709).
_GET_ONLY_INVENTORY_KEYS = ('sku', 'inventoryItemGroupKeys', 'href')
_RETRYABLE_SKIP_REASONS = frozenset({
    'live quantity missing',
    'live quantity unknown',
    'live quantity is 0',
    'live quantity is not positive',
    'live product missing',
    'local image url invalid',
})
_INVENTORY_IMAGE_LIMIT = 24


def _quantity_skip_reason(item: Dict[str, Any]) -> Optional[str]:
    """None 表示 live ship-to quantity 是正数, 可以写.

    缺失、无法解析、以及 <= 0 都不回落到 1.
    """
    avail = item.get('availability') if isinstance(item, dict) else None
    ship = avail.get('shipToLocationAvailability') if isinstance(avail, dict) else None
    if not isinstance(ship, dict) or 'quantity' not in ship:
        return 'live quantity missing'
    raw = ship.get('quantity')
    if isinstance(raw, bool) or raw is None:
        return 'live quantity unknown'
    if isinstance(raw, int):
        qty = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return 'live quantity unknown'
        qty = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return 'live quantity unknown'
        try:
            if any(ch in text for ch in '.eE'):
                number = float(text)
                if not number.is_integer():
                    return 'live quantity unknown'
                qty = int(number)
            else:
                qty = int(text)
        except ValueError:
            return 'live quantity unknown'
    else:
        return 'live quantity unknown'
    if qty == 0:
        return 'live quantity is 0'
    if qty < 0:
        return 'live quantity is not positive'
    return None


def _image_url_drop_oss_process(url: str) -> str:
    """按原始字符串删掉 x-oss-process 这一段 query.

    不用 urlparse/urlencode, 避免把 GigaB2B 的 x-cc/x-cu/x-ct/x-cs
    (包括值里的 '/', '=', '%20') 重新编码. 没有该段时原样返回.
    """
    if not isinstance(url, str) or 'x-oss-process' not in url:
        return url
    hash_at = url.find('#')
    if hash_at >= 0:
        body, fragment = url[:hash_at], url[hash_at:]
    else:
        body, fragment = url, ''
    qmark = body.find('?')
    if qmark < 0:
        return url
    kept: List[str] = []
    dropped = False
    for part in body[qmark + 1:].split('&'):
        key, _sep, _value = part.partition('=')
        if key == 'x-oss-process':
            dropped = True
            continue
        kept.append(part)
    if not dropped:
        return url
    if not kept:
        return body[:qmark] + fragment
    return body[:qmark] + '?' + '&'.join(kept) + fragment


def _omit_nonpositive_package_weight(body: Dict[str, Any]) -> None:
    """去掉 weight.value <= 0. 块里没有别的字段时连 packageWeightAndSize 一起去掉.

    与其它 inventory PUT 一样: eBay 拒收 <= 0 的重量. 正数重量和尺寸原样保留.
    """
    pkg = body.get('packageWeightAndSize')
    if not isinstance(pkg, dict):
        return
    weight = pkg.get('weight')
    if isinstance(weight, dict):
        try:
            value = float(weight.get('value', 0))
        except (TypeError, ValueError):
            pkg.pop('weight', None)
        else:
            if value <= 0:
                pkg.pop('weight', None)
    if not pkg:
        body.pop('packageWeightAndSize', None)


def _inventory_refresh_put_body(item: Dict[str, Any], image_urls: List[str]) -> Dict[str, Any]:
    """深拷贝 live GET, 只替换 product.imageUrls."""
    body = copy.deepcopy(item)
    for key in _GET_ONLY_INVENTORY_KEYS:
        body.pop(key, None)
    body.pop('image_urls', None)
    body.pop('quantity', None)
    product = body.get('product')
    if not isinstance(product, dict):
        raise RuntimeError('live product missing')
    product['imageUrls'] = list(image_urls)
    _omit_nonpositive_package_weight(body)
    return body


def _put_inventory_refresh(client: Any, sku: str, body: Dict[str, Any]) -> None:
    """整单 PUT live 快照. 没有 session 时拒绝写, 不退回局部重建."""
    session = getattr(client, 'session', None)
    base_url = getattr(client, 'base_url', None)
    oauth = getattr(client, 'oauth', None)
    if session is None or not base_url or oauth is None:
        raise RuntimeError(
            f'{sku}: refusing partial inventory write; live session is required')
    token = oauth.get_valid_token()
    locale = body.get('locale')
    if isinstance(locale, str) and locale.strip():
        # GET 的 locale 是 en_US; Content-Language 要 en-US. 只改 header, 不改 body.
        content_language = locale.strip().replace('_', '-')
    else:
        content_language = 'en-US'
    url = f"{str(base_url).rstrip('/')}/sell/inventory/v1/inventory_item/{sku}"
    response = session.put(
        url,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': content_language,
            'Accept': 'application/json',
        },
        json=body,
        timeout=60,
    )
    code = getattr(response, 'status_code', None)
    if code not in (200, 204):
        detail = getattr(response, 'text', '') or ''
        raise RuntimeError(f'inventory PUT HTTP {code}: {detail[:300]}')


def _readback_image_urls(item: Any) -> List[Any]:
    """POST-PUT 读回的 imageUrls. 不清洗、不重编码签名参数, 只取列表本身."""
    if not isinstance(item, dict):
        return []
    product = item.get('product')
    if not isinstance(product, dict):
        return []
    raw = product.get('imageUrls') or []
    return list(raw) if isinstance(raw, list) else []


def _skip_row(sku: str, reason: str, **extra: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {'sku': sku, 'status': 'skipped', 'reason': reason}
    row.update(extra)
    return row


def _skip_is_terminal(row: Dict[str, Any]) -> bool:
    """结构性无操作才出队. qty 未知 / OOS / GET 抖动留在 pending, 明天再试."""
    reason = str(row.get('reason') or '')
    if reason.startswith('live GET failed'):
        return False
    return reason not in _RETRYABLE_SKIP_REASONS


def _normalize_urls_for_compare(urls: List[str]) -> List[str]:
    """Strip only x-oss-process on both sides so a refreshed lead stays healthy."""
    return [
        _image_url_drop_oss_process(url)
        for url in urls
        if isinstance(url, str) and url
    ]


def _should_refresh(local: List[str], live_urls: List[str]) -> Optional[str]:
    """返回 None 表示无需刷新; 否则返回原因."""
    local_n = _normalize_urls_for_compare(local)
    live_n = _normalize_urls_for_compare(live_urls)
    if len(local_n) < 2:
        return None  # 本地都没多图, 没的换
    if not live_n:
        return 'live has no images'
    if len(live_n) <= 1 and len(local_n) >= 2:
        return f'live only {len(live_n)} img vs local {len(local_n)}'
    if local_n[0] != live_n[0]:
        return 'local lead image differs from live'
    return None


GIGA_SIG_KEYS: tuple[str, ...] = ("x-cc", "x-cu", "x-ct", "x-cs")


def live_quantity(item: Dict[str, Any]) -> Optional[int]:
    """Read ship-to qty. Missing/unparseable → None (never default to 1)."""
    raw = ((item.get("availability") or {}).get(
        "shipToLocationAvailability") or {}).get("quantity")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def giga_sig_params(url: str) -> Dict[str, str]:
    """Raw query values (not decoded) so x-c* bytes stay comparable."""
    if not url or not isinstance(url, str) or "?" not in url:
        return {}
    query = url.split("?", 1)[1].split("#", 1)[0]
    out: Dict[str, str] = {}
    for part in query.split("&"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        if key in GIGA_SIG_KEYS and key not in out:
            out[key] = val
    return out


def strip_oss_keep_giga_sig(url: str) -> str:
    """Drop only the raw x-oss-process &-segment; never re-encode the query."""
    if not url or not isinstance(url, str):
        return url or ""
    return _image_url_drop_oss_process(url)


def urls_keep_giga_sigs(source_urls: List[str], dest_urls: List[str]) -> bool:
    """False when a kept URL's x-c* bytes are missing or changed.

    Truncation is allowed: only the dest/kept prefix is compared pairwise.
    """
    n = min(len(source_urls), len(dest_urls))
    for i in range(n):
        src_sig = giga_sig_params(source_urls[i])
        if not src_sig:
            continue
        dest_sig = giga_sig_params(dest_urls[i])
        for key, value in src_sig.items():
            if dest_sig.get(key) != value:
                return False
    return True


def prepare_refresh_image_urls(local_images: List[Any]) -> Tuple[List[str], Optional[str]]:
    """Strip OSS, cap at LIMIT, then check sigs on the kept URLs only."""
    kept = [u for u in local_images if isinstance(u, str) and u][:_INVENTORY_IMAGE_LIMIT]
    prepared = [strip_oss_keep_giga_sig(u) for u in kept]
    if not urls_keep_giga_sigs(kept, prepared):
        return [], "image_sig_stripped"
    return prepared, None


def payload_image_urls(payload: Optional[Dict[str, Any]]) -> List[str]:
    if not payload:
        return []
    aliases = payload.get("image_urls")
    if aliases:
        return list(aliases)
    return list(((payload.get("product") or {}).get("imageUrls")) or [])


def _qty_skip_reason_for_inspect(item: Dict[str, Any]) -> Optional[str]:
    reason = _quantity_skip_reason(item)
    if not reason:
        return None
    if reason in {"live quantity missing", "live quantity unknown"}:
        return "qty_unknown"
    return "qty_zero"


def build_strict_refresh_payload(
    item: Dict[str, Any],
    local: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Dry-run inspect wrapper over #48 PUT-body helpers.

    Never default qty to 1. Keep GigaB2B x-c* params. Weight<=0 is omitted.
    """
    qty_reason = _qty_skip_reason_for_inspect(item)
    if qty_reason:
        return None, qty_reason
    local_images = list(local.get("images") or [])
    prepared, sig_reason = prepare_refresh_image_urls(local_images)
    if sig_reason:
        return None, sig_reason
    try:
        body = _inventory_refresh_put_body(item, prepared)
    except RuntimeError as exc:
        return None, str(exc)
    qty = live_quantity(item)
    body["image_urls"] = prepared
    body["quantity"] = qty
    return body, None


def _refresh_one(client, sku: str, local: Dict[str, Any]) -> Dict[str, Any]:
    try:
        item = client.get_inventory_item(sku)
    except Exception as e:
        return _skip_row(sku, f'live GET failed: {e}')
    if not isinstance(item, dict) or not item:
        return _skip_row(sku, 'no inventory item')

    product = item.get('product') if isinstance(item.get('product'), dict) else None
    local_images = list(local.get('images') or [])
    live_urls = []
    if product is not None:
        raw_live = product.get('imageUrls') or []
        live_urls = raw_live if isinstance(raw_live, list) else []
    refresh_reason = _should_refresh(
        [u for u in local_images if isinstance(u, str)],
        [u for u in live_urls if isinstance(u, str)],
    )
    if not refresh_reason:
        return _skip_row(sku, 'live already healthy', live_n=len(live_urls))

    qty_reason = _quantity_skip_reason(item)
    if qty_reason:
        return _skip_row(sku, qty_reason)
    if product is None:
        return _skip_row(sku, 'live product missing')

    prepared, sig_reason = prepare_refresh_image_urls(local_images)
    if sig_reason:
        return _skip_row(sku, sig_reason)
    if len(prepared) < 2:
        return _skip_row(sku, 'live already healthy', live_n=len(live_urls))

    body = _inventory_refresh_put_body(item, prepared)
    try:
        _put_inventory_refresh(client, sku, body)
    except Exception as e:
        return {'sku': sku, 'status': 'failed', 'reason': f'PUT error: {e}'}

    try:
        item2 = client.get_inventory_item(sku) or {}
    except Exception as e:
        return {'sku': sku, 'status': 'failed', 'reason': f'verification GET failed: {e}'}
    live2 = _readback_image_urls(item2)
    expected_count = len(prepared)
    # 写出几张, 读回就必须几张. 少一张记 failed, 不 mark_done, SKU 留在队列.
    if len(live2) != expected_count:
        return {'sku': sku, 'status': 'failed',
                'reason': 'verification image count mismatch',
                'expected_count': expected_count, 'live_n': len(live2)}
    return {'sku': sku, 'status': 'done', 'reason': refresh_reason,
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
        try:
            local = _load_local_product(sku)
            local_images = local.get('images') or []
            if not local_images:
                row = {'sku': sku, 'status': 'skipped', 'reason': 'no local images'}
            elif not apply_changes:
                row = {'sku': sku, 'status': 'dry_run',
                       'reason': 'would apply', 'local_n': len(local_images)}
            else:
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
        elif row['status'] == 'skipped' and apply_changes and _skip_is_terminal(row):
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
