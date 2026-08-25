"""
Batch Smart Reprice — Terapeak 市场调研后智能重新定价

对所有 PUBLISHED 链接:
1. 按类目/关键词分组
2. 优先使用 Terapeak sold-data (Marketplace Insights)，不可用时明确回退到 Browse
3. 应用智能定价公式:
   - 底价 = 成本 × (1 + 10%)
   - 封顶价 = 成本 × (1 + 35%)
   - 竞争价 = 市场均价 × 0.95
   - 最终价 = max(底价, min(竞争价, 封顶价))
4. 更新 eBay 售价 (Inventory API PUT /offer)
5. 生成详细日志和邮件报告

Usage:
    python scripts/batch_smart_reprice.py                # 干跑 (仅计算, 不改价)
    python scripts/batch_smart_reprice.py --apply        # 实际改价
    python scripts/batch_smart_reprice.py --apply --email  # 改价 + 邮件报告
"""

import os
import sys
import json
import time
import logging
import argparse
import sqlite3
import csv
import warnings
import re
import requests
from requests.exceptions import RequestException, SSLError, Timeout, ConnectionError
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from html import escape as html_escape

PROJECT_ROOT = Path(__file__).parent.parent
REPRICE_EMAIL_LEDGER_DB = PROJECT_ROOT / 'ebay_collection.db'
REPRICE_EMAIL_CLAIM_TTL_SECONDS = 15 * 60
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from src.plugins.terapeak_research.research_client import TerapeakClient
from src.utils.report_images import build_thumbnail_img_html, normalize_thumbnail_url
from src.utils.reprice_sales_cooldown import (
    SALES_COOLDOWN_DAYS,
    SALES_COOLDOWN_MODE,
    cooldown_decision,
    fetch_recently_sold_skus,
    load_sales_cooldown,
)

warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / 'logs' / 'batch_reprice.log', encoding='utf-8'),
    ]
)
log = logging.getLogger("batch_reprice")

TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}


def _ensure_reprice_email_ledger(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_notification_outbox (
            notification_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT
        )
        """
    )


def _reprice_email_claim_is_active(updated_at: object, now: datetime) -> bool:
    """A recent pending row belongs to another sender and cannot be stolen."""
    try:
        claimed_at = datetime.fromisoformat(str(updated_at))
        return (now - claimed_at).total_seconds() < REPRICE_EMAIL_CLAIM_TTL_SECONDS
    except (TypeError, ValueError):
        # A malformed legacy timestamp is not a valid lease; allow recovery.
        return False


def claim_reprice_email_delivery(
    business_date: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Claim the one smart-reprice summary allowed for a business day.

    A completed delivery is never sent again on the same date. Failed SMTP
    attempts remain retryable so a transient mail outage does not suppress the
    report forever.
    """

    business_date = business_date or datetime.now().date().isoformat()
    now_dt = datetime.now()
    now = now_dt.isoformat()
    key = f"smart_reprice:{business_date}"
    target = Path(db_path) if db_path else REPRICE_EMAIL_LEDGER_DB
    with sqlite3.connect(str(target)) as conn:
        _ensure_reprice_email_ledger(conn)
        conn.commit()
        # SQLite serializes writers here. Combined with the pending lease below,
        # two watchdog/recovery processes cannot both reach SMTP for one date.
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            "SELECT status, updated_at FROM task_notification_outbox WHERE notification_key = ?",
            (key,),
        ).fetchone()
        if row and str(row[0]).lower() == 'sent':
            conn.rollback()
            return False
        if row and str(row[0]).lower() == 'pending' and _reprice_email_claim_is_active(
            row[1], now_dt
        ):
            conn.rollback()
            return False
        if row:
            conn.execute(
                "UPDATE task_notification_outbox SET status = ?, updated_at = ?, last_error = NULL "
                "WHERE notification_key = ?",
                ('pending', now, key),
            )
        else:
            conn.execute(
                "INSERT INTO task_notification_outbox "
                "(notification_key, status, created_at, updated_at, last_error) VALUES (?,?,?,?,NULL)",
                (key, 'pending', now, now),
            )
        conn.commit()
    return True


def mark_reprice_email_delivery(
    business_date: str | None = None,
    delivered: bool = False,
    error: str = '',
    db_path: Path | None = None,
) -> None:
    business_date = business_date or datetime.now().date().isoformat()
    key = f"smart_reprice:{business_date}"
    target = Path(db_path) if db_path else REPRICE_EMAIL_LEDGER_DB
    now = datetime.now().isoformat()
    with sqlite3.connect(str(target)) as conn:
        _ensure_reprice_email_ledger(conn)
        conn.execute(
            "UPDATE task_notification_outbox SET status = ?, updated_at = ?, last_error = ? "
            "WHERE notification_key = ?",
            ('sent' if delivered else 'failed', now, '' if delivered else str(error)[:500], key),
        )
        conn.commit()


def _format_margin_pct(value) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return ""


def _format_money(value) -> str:
    try:
        return f"${float(value):.2f}"
    except Exception:
        return "$0.00"


def _extract_first_image_url(raw_images) -> str:
    if not raw_images:
        return ""

    images = raw_images
    if isinstance(raw_images, str):
        try:
            images = json.loads(raw_images)
        except Exception:
            images = [raw_images]

    if isinstance(images, list):
        for image in images:
            url = normalize_thumbnail_url(image)
            if url.startswith("http"):
                return url
    else:
        url = normalize_thumbnail_url(images)
        return url if url.startswith("http") else ""

    return ""


def _build_price_reason(result: dict) -> str:
    strategy = result.get('strategy') or ''
    market_avg = float(result.get('market_avg') or 0)
    total_cost = float(result.get('total_cost') or 0)
    new_price = float(result.get('new_price') or 0)
    floor_price = float(result.get('floor_price') or 0)
    ceiling_price = float(result.get('ceiling_price') or 0)
    competitive_price = result.get('competitive_price')
    market_source = result.get('market_source') or 'UNKNOWN'
    sample_size = int(result.get('market_sample_size') or 0)
    sold_count = int(result.get('market_sold_count') or 0)
    fallback_reason = result.get('market_fallback_reason') or ''

    source_note = f"来源 {market_source}"
    if sold_count:
        source_note += f"，售出样本 {sold_count}"
    elif sample_size:
        source_note += f"，样本 {sample_size}"
    if fallback_reason:
        source_note += f"，回退原因: {fallback_reason}"

    if market_avg <= 0:
        return (
            f"无可用市场均价，按成本 {_format_money(total_cost)} 和标准 15% 利润计算，"
            f"建议价 {_format_money(new_price)}。{source_note}。"
        )

    competitive_note = (
        f"竞争价 {_format_money(competitive_price)}"
        if competitive_price is not None else "无竞争价"
    )

    if strategy == 'FLOOR_PRICE':
        return (
            f"市场均价 {_format_money(market_avg)}，{competitive_note} 低于保底价 "
            f"{_format_money(floor_price)}；为保护最低利润，采用 {_format_money(new_price)}。"
            f"{source_note}。"
        )
    if strategy == 'CEILING_PRICE':
        return (
            f"市场均价 {_format_money(market_avg)}，{competitive_note} 高于封顶价 "
            f"{_format_money(ceiling_price)}；为避免定价过高，采用 {_format_money(new_price)}。"
            f"{source_note}。"
        )
    if strategy == 'ANTI_LOSS':
        return (
            f"市场均价 {_format_money(market_avg)}，计算价触发防亏损保护；"
            f"按成本 {_format_money(total_cost)} 采用 {_format_money(new_price)}。{source_note}。"
        )
    if strategy == 'COMPETITIVE':
        return (
            f"市场均价 {_format_money(market_avg)}，按市场约 95% 的{competitive_note}定价，"
            f"最终价 {_format_money(new_price)}。{source_note}。"
        )

    return (
        f"按策略 {strategy or 'UNKNOWN'} 计算，成本 {_format_money(total_cost)}，"
        f"市场均价 {_format_money(market_avg)}，建议价 {_format_money(new_price)}。{source_note}。"
    )


def _write_change_reports(results, timestamp_label: str):
    """Save changed-price details in CSV and HTML for quick review."""
    changed = [r for r in results if r.get('status') == 'updated' and r.get('current_price') is not None]
    if not changed:
        return None, None

    reports_dir = PROJECT_ROOT / 'reports'
    reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = reports_dir / f'reprice_changes_{timestamp_label}.csv'
    html_path = reports_dir / f'reprice_changes_{timestamp_label}.html'

    changed_sorted = sorted(
        changed,
        key=lambda r: (abs(float(r.get('price_diff') or 0)), str(r.get('sku') or '')),
        reverse=True,
    )

    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'sku', 'title', 'image_url', 'old_price', 'new_price', 'price_diff', 'pct_change',
            'margin_pct', 'strategy', 'total_cost', 'market_avg', 'market_source',
            'market_sample_size', 'market_sold_count', 'price_reason'
        ])
        for r in changed_sorted:
            writer.writerow([
                r.get('sku', ''),
                r.get('title', ''),
                r.get('image_url', ''),
                r.get('current_price', ''),
                r.get('new_price', ''),
                r.get('price_diff', ''),
                r.get('pct_change', ''),
                _format_margin_pct(r.get('margin')),
                r.get('strategy', ''),
                r.get('total_cost', ''),
                r.get('market_avg', ''),
                r.get('market_source', ''),
                r.get('market_sample_size', ''),
                r.get('market_sold_count', ''),
                r.get('price_reason', ''),
            ])

    html_rows = []
    for r in changed_sorted:
        diff = float(r.get('price_diff') or 0)
        diff_color = '#067647' if diff > 0 else '#b42318'
        title = html_escape(str(r.get('title', '') or ''))
        sku = html_escape(str(r.get('sku', '') or ''))
        reason = html_escape(str(r.get('price_reason', '') or ''))
        thumb = build_thumbnail_img_html(r.get('image_url', ''), width=48, height=48)
        html_rows.append(
            "<tr>"
            f"<td style='text-align:center'>{thumb}</td>"
            f"<td><b>{sku}</b><br><span class='title'>{title[:120]}</span></td>"
            f"<td>${float(r.get('current_price') or 0):.2f}</td>"
            f"<td>${float(r.get('new_price') or 0):.2f}</td>"
            f"<td style='color:{diff_color}'>{float(r.get('pct_change') or 0):+.1f}%</td>"
            f"<td>{_format_margin_pct(r.get('margin'))}</td>"
            f"<td>{r.get('strategy', '')}</td>"
            f"<td>${float(r.get('total_cost') or 0):.2f}</td>"
            f"<td>${float(r.get('market_avg') or 0):.2f}</td>"
            f"<td>{r.get('market_source', '')}</td>"
            f"<td>{reason}</td>"
            "</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Smart Reprice Changes {timestamp_label}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border: 1px solid #d0d5dd; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f2f4f7; }}
    .meta {{ margin-bottom: 16px; color: #475467; }}
    .title {{ color: #475467; font-size: 12px; }}
  </style>
</head>
<body>
  <h2>Smart Reprice Changes</h2>
  <div class="meta">Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Changed SKUs: {len(changed_sorted)}</div>
  <table>
    <tr>
      <th>Image</th>
      <th>Product</th>
      <th>Old Price</th>
      <th>New Price</th>
      <th>Change</th>
      <th>Margin</th>
      <th>Strategy</th>
      <th>Cost</th>
      <th>Market Avg</th>
      <th>Source</th>
      <th>Reason</th>
    </tr>
    {''.join(html_rows)}
  </table>
</body>
</html>
"""
    html_path.write_text(html, encoding='utf-8')
    return csv_path, html_path


def _backoff_sleep(attempt: int, base: float = 1.5, cap: float = 12.0):
    time.sleep(min(cap, base * attempt))


def _is_transient_http_error(status_code: int) -> bool:
    return status_code in TRANSIENT_HTTP_STATUS


def _build_changed_rows(results):
    """Normalize changed-price rows for downstream daily reporting."""
    changed_rows = []
    for row in results:
        if row.get('status') != 'updated' or row.get('current_price') is None:
            continue
        changed_rows.append({
            'sku': row.get('sku', ''),
            'title': row.get('title', ''),
            'image_url': row.get('image_url', ''),
            'old_price': float(row.get('current_price') or 0),
            'new_price': float(row.get('new_price') or 0),
            'price_diff': float(row.get('price_diff') or 0),
            'pct_change': float(row.get('pct_change') or 0),
            'strategy': row.get('strategy', ''),
            'market_source': row.get('market_source', ''),
            'market_avg': float(row.get('market_avg') or 0),
            'market_sample_size': int(row.get('market_sample_size') or 0),
            'market_sold_count': int(row.get('market_sold_count') or 0),
            'market_fallback_reason': row.get('market_fallback_reason') or '',
            'price_reason': row.get('price_reason') or '',
        })

    changed_rows.sort(key=lambda r: (abs(r['price_diff']), r['sku']), reverse=True)
    return changed_rows


# ═══════════════════════════════════════════════════════════════
# Category → search keywords for market research
# ═══════════════════════════════════════════════════════════════
CATEGORY_SEARCH_KEYWORDS = {
    # Sofas
    "38208": "sofa couch",
    # Bed Frames
    "175758": "bed frame",
    "131604": "bed frame",
    # Bunk Beds
    "175754": "bunk bed",
    # Mattresses
    "131588": "mattress",
    # Dining Sets
    "107578": "dining table set",
    "177816": "dining table set",
    # Dining Chairs
    "25458": "dining chair set",
    # Bar Stools
    "183316": "bar stool set",
    # Bar Tables / legacy alias
    "177815": "bar table counter height",
    # Coffee Tables
    "38204": "coffee table",
    # Console Tables
    "38205": "console table",
    # End Tables
    "38200": "end table side table",
    # Nightstands
    "38199": "nightstand",
    # Desks
    "88057": "desk",
    "30889": "desk",
    # Office Chairs
    "54235": "office chair",
    # TV Stands
    "20488": "tv stand",
    # Cabinets
    "20487": "storage cabinet",
    # Bookcases
    "3199": "bookcase bookshelf",
    # Wardrobes
    "103430": "wardrobe armoire",
    "68240": "wardrobe armoire",
    # Dressers
    "114397": "dresser",
    "20466": "dresser",
    # Jewelry Organizers
    "262017": "jewelry organizer cabinet",
    # Display Cabinets
    "20493": "display cabinet curio",
    # Sideboards
    "63557": "sideboard buffet",
    # Wine Racks
    "45331": "wine rack bar cabinet",
    # Shoe Storage
    "38221": "shoe rack storage",
    # Kitchen Islands
    "177000": "kitchen island",
    # Kitchen Carts
    "115753": "kitchen cart microwave stand",
    # Bathroom Vanities
    "32878": "bathroom vanity",
    # Accent Chairs
    "118218": "accent chair lounge chair",
    # Rocking Chairs
    "20877": "rocking chair glider",
    # Ottomans
    "175761": "ottoman footstool",
    # Patio Chairs
    "79682": "patio chair outdoor",
    # Patio Sets
    "25863": "patio furniture set outdoor",
    # Outdoor Tables
    "79686": "patio table outdoor",
    # Dog Crates
    "121851": "dog crate kennel",
    # Pet Hutches / Chicken Coops
    "63108": "outdoor pet hutch coop",
    # Cat Trees / Cat Furniture
    "20740": "cat tree tower",
    # Dog Beds
    "20744": "dog bed orthopedic",
    # Dog Houses
    "108884": "dog house outdoor",
    # Treadmills
    "15280": "treadmill walking pad",
    # Exercise Bikes
    "58102": "exercise bike stationary",
    # Trampolines
    "57275": "trampoline outdoor",
    # Hall Trees
    "22656": "hall tree entryway",
    # Storage Benches
    "103431": "storage bench entryway",
    # Headboards
    "175756": "headboard upholstered",
    # Mattress Toppers
    "175751": "mattress topper",
    # Bed Frames (alt)
    "175758": "platform bed frame",
    # Room Dividers
    "175764": "room divider screen",
    # Mirrors
    "20580": "wall mirror floor mirror",
    # Clothes Racks
    "175755": "clothes rack garment rack",
    # Suitcases
    "16289": "luggage suitcase set",
    # Bathroom Fixtures
    "115625": "bathroom fixture",
    # Sports Nets
    "50876": "golf net practice",
}


def get_ebay_client():
    """初始化 eBay 客户端"""
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_policy_manager import EbayPolicyManager
    from src.clients.real_ebay_client import RealEbayClient

    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    pm = EbayPolicyManager(oauth)
    client = RealEbayClient(oauth, pm)
    return client, oauth


def get_published_products():
    """获取所有 PUBLISHED 产品及成本数据"""
    db_path = PROJECT_ROOT / "ebay_collection.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT sku, title, images, cost_breakdown, optimization, listing_id
        FROM collected_products
        WHERE status = 'PUBLISHED'
          AND cost_breakdown IS NOT NULL
        ORDER BY sku
    """).fetchall()

    products = []
    for r in rows:
        cost = json.loads(r['cost_breakdown']) if r['cost_breakdown'] else {}
        opt = json.loads(r['optimization']) if r['optimization'] else {}
        total_cost = cost.get('total_dajian_cost', 0)
        if total_cost <= 0:
            continue

        cat_id = str(opt.get('categoryId', ''))
        title = opt.get('title', '') or r['title'] or ''

        products.append({
            'sku': r['sku'],
            'title': title,
            'image_url': _extract_first_image_url(r['images']),
            'category_id': cat_id,
            'total_cost': total_cost,
            'listing_id': r['listing_id'],
            'cost_breakdown': cost,
        })

    conn.close()
    return products


def fetch_market_price(market_client: TerapeakClient, keyword: str, category_id: str = None,
                       min_price: float = None) -> dict:
    """
    优先使用 Terapeak sold-data，必要时回退到 Browse

    Returns:
        {'avg_price': float, 'median_price': float, 'total_listings': int, ...}
    """
    return market_client.get_market_price_snapshot(
        keywords=keyword,
        category_id=category_id,
        min_price=min_price,
        sold_days=90,
        buy_it_now_only=True,
    )


def smart_price(total_cost: float, market_avg: float,
                min_margin: float = 0.10, max_margin: float = 0.35) -> dict:
    """
    智能定价 — 委托到 PricingEngine.calculate_smart_price (单一真相源).

    历史 (2026-05): 此函数曾内嵌一份完整公式拷贝, 与 PricingEngine 重复实现.
    现已统一委托, 避免漂移. 返回字段做了 key 重映射以保持下游 (邮件报告 / CSV)
    兼容: listing_price ← final_price, market_avg ← market_price.
    """
    from src.services.pricing_engine import PricingEngine
    r = PricingEngine.calculate_smart_price(
        total_cost=total_cost,
        market_price=market_avg,
        min_margin=min_margin,
        max_margin=max_margin,
    )
    return {
        'listing_price': r['final_price'],
        'floor_price': r['floor_price'],
        'ceiling_price': r['ceiling_price'],
        'competitive_price': r.get('competitive_price'),
        'market_avg': r.get('market_price') or 0,
        'strategy': r['strategy'],
        'margin': r['margin'],
    }


def _select_best_offer(offers, expected_listing_id: str = None):
    """Prefer the live offer for the current listing over stale leftovers."""
    if not offers:
        return None

    expected_listing_id = str(expected_listing_id or '')

    def _rank(offer):
        listing = offer.get('listing') or {}
        listing_id = str(listing.get('listingId') or '')
        listing_status = listing.get('listingStatus', '')
        status = offer.get('status', '')
        marketplace = offer.get('marketplaceId', '')
        listing_match_rank = 0 if expected_listing_id and listing_id == expected_listing_id else 1
        active_rank = 0 if listing_status == 'ACTIVE' else 1
        published_rank = 0 if status == 'PUBLISHED' else 1
        marketplace_rank = 0 if marketplace == 'EBAY_US' else 1
        return (listing_match_rank, active_rank, published_rank, marketplace_rank, offer.get('offerId', ''))

    return sorted(offers, key=_rank)[0]


def _trading_site_id() -> str:
    from src.utils.store_profile import get_store_profile

    profile = get_store_profile()
    return str(getattr(profile, "ebay_site_id", "0") or "0")


def _trading_headers(token: str, call: str, site_id: str) -> dict:
    return {
        "X-EBAY-API-CALL-NAME": call,
        "X-EBAY-API-SITEID": site_id,
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
        "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": "text/xml",
    }


def fetch_trading_item_price(oauth, listing_id: str):
    """Read live Trading price via GetItem."""
    from xml.sax.saxutils import escape as xml_escape

    from src.services.motors_trading import parse_trading_item_price

    listing_id = str(listing_id or "").strip()
    if not listing_id:
        return None
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{xml_escape(listing_id)}</ItemID>"
        "</GetItemRequest>"
    )
    try:
        response = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers=_trading_headers(oauth.get_valid_token(), "GetItem", _trading_site_id()),
            data=xml.encode("utf-8"),
            timeout=30,
            verify=False,
        )
        if response.status_code != 200:
            log.warning(f"  GetItem {listing_id}: HTTP {response.status_code}")
            return None
        return parse_trading_item_price(response.content.decode("utf-8", "replace"))
    except (SSLError, Timeout, ConnectionError, RequestException) as e:
        log.warning(f"  GetItem {listing_id}: {e}")
        return None
    except Exception as e:
        log.warning(f"  GetItem {listing_id}: {e}")
        return None


def update_trading_price(oauth, sku: str, new_price: float, listing_id: str) -> bool:
    """Write a Trading/Motors listing price via ReviseFixedPriceItem.

    Price-only revise: do not send ItemSpecifics, or the live fitment/aspects
    would be replaced. Furniture/Inventory listings never enter this path.
    """
    from src.services.motors_trading import build_revise_fixed_price_item_xml

    listing_id = str(listing_id or "").strip()
    if not listing_id:
        log.error(f"  {sku}: Trading revise needs listing_id")
        return False
    xml = build_revise_fixed_price_item_xml(
        item_id=listing_id,
        start_price=new_price,
    )
    try:
        response = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers=_trading_headers(
                oauth.get_valid_token(), "ReviseFixedPriceItem", _trading_site_id()
            ),
            data=xml.encode("utf-8"),
            timeout=40,
            verify=False,
        )
        body = response.content.decode("utf-8", "replace")
        if response.status_code != 200:
            log.error(f"  {sku}: Trading revise failed HTTP {response.status_code}")
            return False
        ack_match = re.search(r"<Ack>(\w+)</Ack>", body)
        ack = ack_match.group(1) if ack_match else ""
        if ack in {"Success", "Warning"}:
            return True
        message = re.search(r"<LongMessage>(.*?)</LongMessage>", body)
        log.error(
            f"  {sku}: Trading revise failed: "
            f"{(message.group(1) if message else body)[:160]}"
        )
        return False
    except (SSLError, Timeout, ConnectionError, RequestException) as e:
        log.error(f"  {sku}: Trading revise exception {e}")
        return False
    except Exception as e:
        log.error(f"  {sku}: exception {e}")
        return False


def read_current_listing_price(oauth, sku: str, listing_id: str = None, client=None):
    """Read the live listing price from the listing's write channel."""
    from src.services.repricing_guard import reprice_write_channel

    if reprice_write_channel(listing_id) == "trading":
        try:
            return fetch_trading_item_price(oauth, listing_id)
        except Exception:
            return None
    if client is None:
        return None
    try:
        offers = client.get_offers_by_sku(sku)
        if not offers:
            return None
        offer = _select_best_offer(offers, expected_listing_id=listing_id)
        if not offer:
            return None
        return float(offer.get("pricingSummary", {}).get("price", {}).get("value", 0) or 0)
    except Exception:
        return None


def update_ebay_price(oauth, sku: str, new_price: float, expected_listing_id: str = None) -> bool:
    """Update live eBay price through the listing's write channel.

    Trading-channel Motors listings use ReviseFixedPriceItem. Furniture and
    other Inventory stores keep the existing offer PUT path unchanged.

    🛡️ 安全护栏 (2026-05): 写出前调用 repricing_guard.precheck_price 校验.
    - 价格 < 带广告死线 但 >= 关广告死线 → 自动关广告后放行
    - 价格 < 关广告死线 (即真亏本) → 拒绝, 不调任何 eBay API
    """
    # ── 守门员: 死线 + 广告自适应 (与 InventorySyncService 共用) ──
    try:
        from src.services.repricing_guard import precheck_price, reprice_write_channel
        ok, reason = precheck_price(
            sku=sku, new_price=new_price,
            db_path='ebay_collection.db',
            listing_id=expected_listing_id,
            allow_ad_disable=True,
        )
        if not ok:
            log.error(f"  {sku}: 🚫 PRICE GUARD 拒绝改价 ({reason})")
            return False
    except Exception as guard_exc:
        log.warning(f"  {sku}: ⚠️ PRICE GUARD 异常, 放行: {guard_exc}")
        from src.services.repricing_guard import reprice_write_channel

    if reprice_write_channel(expected_listing_id) == "trading":
        try:
            return update_trading_price(oauth, sku, new_price, expected_listing_id)
        except Exception as e:
            log.error(f"  {sku}: exception {e}")
            return False

    token = oauth.get_valid_token()
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Content-Language': 'en-US',
    }

    # GET offer
    url = f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}"
    last_error = None
    for get_attempt in range(1, 4):
        try:
            r = requests.get(url, headers=headers, timeout=30, verify=False)
            if r.status_code != 200:
                last_error = f"GET offer failed {r.status_code}"
                if _is_transient_http_error(r.status_code) and get_attempt < 3:
                    log.warning(f"  {sku}: {last_error}, retry {get_attempt}/3")
                    _backoff_sleep(get_attempt)
                    continue
                log.error(f"  {sku}: {last_error}")
                return False

            offers = r.json().get('offers', [])
            if not offers:
                log.error(f"  {sku}: no offer found")
                return False

            offer = _select_best_offer(offers, expected_listing_id=expected_listing_id)
            if not offer:
                log.error(f"  {sku}: no matching offer found")
                return False
            offer_id = offer['offerId']

            # Update price
            offer['pricingSummary']['price']['value'] = str(round(new_price, 2))
            put_url = f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}"

            for put_attempt in range(1, 4):
                try:
                    r = requests.put(put_url, headers=headers, json=offer, timeout=60, verify=False)

                    if r.status_code in (200, 204):
                        return True

                    err_msg = ''
                    try:
                        err_data = r.json()
                        err_msg = err_data.get('errors', [{}])[0].get('message', '')
                    except Exception:
                        err_msg = r.text[:200]

                    is_promo_block = ('sale' in err_msg.lower()
                                     or 'promotion' in err_msg.lower()
                                     or 'markdown' in err_msg.lower())

                    if is_promo_block:
                        log.warning(f"  {sku}: promotion blocking price update, "
                                   f"trying promotion-aware update...")
                        try:
                            from src.services.ebay_discount_service import EbayDiscountService
                            disc_svc = EbayDiscountService()
                            pa_result = disc_svc.update_price_through_promotion(sku, new_price)
                            if pa_result.get('price_updated'):
                                promo_info = ('restored' if pa_result.get('promotion_restored')
                                             else 'needs manual restore')
                                log.info(f"  {sku}: promotion-aware update OK "
                                        f"(promo {promo_info})")
                                return True
                            else:
                                log.error(f"  {sku}: promotion-aware update failed: "
                                         f"{pa_result.get('error', '?')}")
                                return False
                        except Exception as e:
                            log.error(f"  {sku}: promotion-aware exception: {e}")
                            return False

                    last_error = f"PUT offer failed {r.status_code}: {err_msg[:150]}"
                    if _is_transient_http_error(r.status_code) and put_attempt < 3:
                        log.warning(f"  {sku}: {last_error}, retry {put_attempt}/3")
                        _backoff_sleep(put_attempt)
                        continue

                    log.error(f"  {sku}: {last_error}")
                    return False
                except (SSLError, Timeout, ConnectionError, RequestException) as e:
                    last_error = str(e)
                    if put_attempt < 3:
                        log.warning(f"  {sku}: PUT retry {put_attempt}/3 after transient error: {e}")
                        _backoff_sleep(put_attempt)
                        continue
                    log.error(f"  {sku}: exception {e}")
                    return False

            return False
        except (SSLError, Timeout, ConnectionError, RequestException) as e:
            last_error = str(e)
            if get_attempt < 3:
                log.warning(f"  {sku}: GET retry {get_attempt}/3 after transient error: {e}")
                _backoff_sleep(get_attempt)
                continue
            log.error(f"  {sku}: exception {e}")
            return False
        except Exception as e:
            log.error(f"  {sku}: exception {e}")
            return False

    if last_error:
        log.error(f"  {sku}: {last_error}")
    return False


def fetch_live_offer_price(oauth, sku: str, expected_listing_id: str = None):
    """Fetch the current live price for a SKU (Inventory offer or Trading GetItem)."""
    try:
        from src.services.repricing_guard import reprice_write_channel

        if reprice_write_channel(expected_listing_id) == "trading":
            return fetch_trading_item_price(oauth, expected_listing_id)
        token = oauth.get_valid_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }
        url = f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}"
        resp = requests.get(url, headers=headers, timeout=60, verify=False)
        if resp.status_code != 200:
            return None
        offers = resp.json().get('offers', [])
        if not offers:
            return None
        offer = _select_best_offer(offers, expected_listing_id=expected_listing_id)
        if not offer:
            return None
        price_value = offer.get('pricingSummary', {}).get('price', {}).get('value')
        return float(price_value) if price_value is not None else None
    except Exception:
        return None


def verify_ebay_price_update(oauth, sku: str, expected_price: float, expected_listing_id: str = None,
                             attempts: int = 2, tolerance: float = 0.05):
    """Verify that the live offer price matches the requested price."""
    live_price = None
    for attempt in range(1, attempts + 1):
        live_price = fetch_live_offer_price(oauth, sku, expected_listing_id=expected_listing_id)
        if live_price is not None and abs(live_price - expected_price) <= tolerance:
            return True, live_price
        if attempt < attempts:
            _backoff_sleep(attempt, base=0.75, cap=2.0)
    return False, live_price


def generate_search_keyword(product: dict) -> str:
    """
    根据产品标题和类目生成搜索关键词

    策略:
    1. 如果类目有预设关键词，使用预设
    2. 否则从标题提取关键词 (取前3-4个有意义的词)
    """
    cat_id = product.get('category_id', '')
    if cat_id in CATEGORY_SEARCH_KEYWORDS:
        return CATEGORY_SEARCH_KEYWORDS[cat_id]

    # 从标题提取
    title = product.get('title', '').lower()
    # 去掉尺寸、数字、常见修饰词
    noise = {'with', 'and', 'for', 'the', 'set', 'of', 'in', 'inch', 'new',
             'modern', 'large', 'small', 'heavy', 'duty', 'home', 'pack', 'pcs',
             'piece', 'adjustable', 'portable', 'foldable', 'folding', 'black',
             'white', 'brown', 'gray', 'grey', 'wood', 'metal', 'fabric'}
    words = []
    for w in title.split():
        w_clean = w.strip('.,()[]')
        if w_clean and not w_clean.isdigit() and w_clean not in noise and len(w_clean) > 2:
            words.append(w_clean)
        if len(words) >= 4:
            break
    return ' '.join(words) if words else title[:40]


def run_batch_reprice(dry_run: bool = True, send_email: bool = False,
                      market_mode: str = None, from_cro_queue: bool = False):
    """主函数"""
    log.info("=" * 70)
    log.info("BATCH SMART REPRICE — Terapeak 市场调研 + 智能定价")
    log.info("=" * 70)
    log.info(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")

    products = get_published_products()
    log.info(f"Loaded {len(products)} PUBLISHED products with cost data")

    cro_queue_skus: set = set()
    if from_cro_queue:
        try:
            from src.services.cro_action_queue import load_pending
            pend = load_pending(action_type='price_drop')
            cro_queue_skus = {r['sku'] for r in pend if r.get('priority') == 1}
            log.info(f"CRO queue: {len(cro_queue_skus)} P1 price_drop SKUs pending")
            if cro_queue_skus:
                products = [p for p in products if p.get('sku') in cro_queue_skus]
                log.info(f"Filtered to {len(products)} products from CRO queue")
            else:
                log.info("CRO queue empty, nothing to do.")
                products = []
        except Exception as e:
            log.warning(f"Failed to load CRO queue: {e}")

    if not products:
        log.info("No products to reprice.")
        return {
            'status': 'ok',
            'timestamp': datetime.now().isoformat(),
            'mode': 'dry_run' if dry_run else 'apply',
            'market_mode': market_mode or 'auto',
            'marketplace_insights_access': {},
            'path': None,
            'change_reports': {'csv': None, 'html': None},
            'summary': {
                'total': 0,
                'price_changes': 0,
                'price_up': 0,
                'price_down': 0,
                'no_change': 0,
                'held_recent_sale': 0,
                'no_market_data': 0,
                'errors': 0,
                'strategies': {},
                'market_sources': {},
            },
            'cooldown': {'available': False, 'days': SALES_COOLDOWN_DAYS,
                         'mode': SALES_COOLDOWN_MODE, 'sku_count': 0,
                         'error': 'no_products'},
            'market_research': {},
            'results': [],
            'changed_rows': [],
        }

    client, oauth = get_ebay_client()
    market_client = TerapeakClient()
    if market_mode:
        market_client.market_mode = market_mode
    log.info(f"Market data mode: {market_client.market_mode}")
    if market_client.market_mode == 'browse_only':
        insights_probe = {
            'available': False,
            'reason': 'disabled_by_market_mode',
            'status_code': None,
            'auth_mode': None,
            'endpoint': None,
            'error': None,
        }
        log.info("Marketplace Insights probe skipped because market mode is browse_only")
    else:
        insights_probe = market_client.check_marketplace_insights_access()
        if insights_probe.get('available'):
            log.info(f"Marketplace Insights access: OK via {insights_probe.get('auth_mode')} {insights_probe.get('endpoint')}")
        else:
            log.warning(
                "Marketplace Insights unavailable: reason=%s status=%s error=%s",
                insights_probe.get('reason'),
                insights_probe.get('status_code'),
                insights_probe.get('error'),
            )

    # ── Sales cooldown guard: protect SKUs that recently converted ──
    recent_sold_skus, cooldown_meta = load_sales_cooldown()
    if SALES_COOLDOWN_DAYS <= 0:
        log.info("Sales cooldown guard: DISABLED (REPRICE_SALES_COOLDOWN_DAYS=0)")
    elif cooldown_meta.get('available'):
        log.info(
            f"Sales cooldown guard: {len(recent_sold_skus)} SKUs sold in last "
            f"{SALES_COOLDOWN_DAYS}d protected (mode={SALES_COOLDOWN_MODE})"
        )
    else:
        log.warning(
            "⚠️ Sales cooldown guard INACTIVE: could not load recent sales "
            f"(error={cooldown_meta.get('error')}); repricing ALL SKUs (legacy behaviour)"
        )

    # Group products by category for efficient market research
    by_cat = defaultdict(list)
    for p in products:
        by_cat[p['category_id']].append(p)

    log.info(f"Products distributed across {len(by_cat)} categories")

    # Market research cache (category → market data)
    market_cache = {}
    results = []

    total = len(products)
    processed = 0
    price_changes = 0
    price_up = 0
    price_down = 0
    errors = 0
    no_market = 0
    held_recent_sale = 0

    # Phase 1: Market research by category
    log.info("\n" + "=" * 50)
    log.info("PHASE 1: Market Research (Terapeak sold-data preferred)")
    log.info("=" * 50)

    for cat_id, prods in by_cat.items():
        keyword = CATEGORY_SEARCH_KEYWORDS.get(cat_id, '')
        if not keyword:
            # 从这个类目的第一个产品标题提取
            keyword = generate_search_keyword(prods[0])

        log.info(f"  Cat {cat_id} ({len(prods)} products): searching '{keyword}'...")
        mkt = fetch_market_price(market_client, keyword, category_id=cat_id)
        market_cache[cat_id] = mkt

        if mkt.get('avg_price', 0) > 0:
            source = mkt.get('source', 'UNKNOWN')
            sold_count = mkt.get('sold_count', 0)
            sold_note = f", sold={sold_count}" if sold_count else ""
            fallback_note = f", fallback={mkt.get('fallback_reason')}" if mkt.get('fallback_reason') else ""
            log.info(f"    → avg=${mkt['avg_price']:.2f}, median=${mkt.get('median_price', 0):.2f}, "
                     f"range=${mkt.get('min_price', 0):.2f}-${mkt.get('max_price', 0):.2f}, "
                     f"{mkt.get('total_listings', 0)} samples, source={source}{sold_note}{fallback_note}")
        else:
            log.warning(f"    → No market data (source={mkt.get('source', 'UNKNOWN')}, "
                        f"fallback={mkt.get('fallback_reason')}, error={mkt.get('error', 'unknown')})")

        # Keep a conservative pace across Terapeak/Browse research calls.
        time.sleep(1)

    # Phase 2: Calculate new prices and apply
    log.info("\n" + "=" * 50)
    log.info("PHASE 2: Smart Pricing + Price Update")
    log.info("=" * 50)

    for p in products:
        processed += 1
        sku = p['sku']
        total_cost = p['total_cost']
        cat_id = p['category_id']

        # Get market data
        mkt = market_cache.get(cat_id, {})
        market_avg = mkt.get('avg_price', 0)

        # Calculate smart price
        pricing = smart_price(total_cost, market_avg)
        new_price = pricing['listing_price']
        strategy = pricing['strategy']

        if market_avg <= 0:
            no_market += 1

        current_price = read_current_listing_price(
            oauth, sku, p.get("listing_id"), client=client
        )

        # Calculate price change
        price_diff = new_price - (current_price or 0) if current_price else 0
        pct_change = (price_diff / current_price * 100) if current_price and current_price > 0 else 0

        result = {
            'sku': sku,
            'title': p['title'][:60],
            'image_url': p.get('image_url', ''),
            'category_id': cat_id,
            'total_cost': total_cost,
            'current_price': current_price,
            'new_price': new_price,
            'floor_price': pricing.get('floor_price'),
            'ceiling_price': pricing.get('ceiling_price'),
            'competitive_price': pricing.get('competitive_price'),
            'market_avg': market_avg,
            'market_source': mkt.get('source'),
            'market_endpoint': mkt.get('endpoint'),
            'market_auth_mode': mkt.get('auth_mode'),
            'market_fallback_reason': mkt.get('fallback_reason'),
            'market_sample_size': mkt.get('sample_size', 0),
            'market_sold_count': mkt.get('sold_count', 0),
            'strategy': strategy,
            'margin': pricing['margin'],
            'price_diff': round(price_diff, 2),
            'pct_change': round(pct_change, 1),
            'status': 'pending',
        }
        result['price_reason'] = _build_price_reason(result)

        # Skip if no significant price change (< $0.50 or < 1%)
        if current_price and abs(price_diff) < 0.50 and abs(pct_change) < 1.0:
            result['status'] = 'no_change'
            results.append(result)
            continue

        # Sales cooldown: don't disturb a listing that just proved its price.
        allow_change, hold_reason = cooldown_decision(
            sku in recent_sold_skus, price_diff, SALES_COOLDOWN_MODE
        )
        if not allow_change:
            result['status'] = 'held_recent_sale'
            result['hold_reason'] = hold_reason
            held_recent_sale += 1
            log.info(
                f"  [{processed}/{total}] {sku}: HELD @ ${current_price or '?'} "
                f"(sold within {SALES_COOLDOWN_DAYS}d, {hold_reason}); "
                f"skipped move to ${new_price:.2f}"
            )
            results.append(result)
            continue

        if price_diff > 0:
            direction = "↑"
            price_up += 1
        else:
            direction = "↓"
            price_down += 1
        price_changes += 1

        log.info(f"  [{processed}/{total}] {sku}: ${current_price or '?'} → ${new_price:.2f} "
                 f"({direction}{abs(pct_change):.1f}%) [{strategy}] "
                 f"cost=${total_cost:.2f} mkt=${market_avg:.2f} "
                 f"src={mkt.get('source', 'UNKNOWN')} margin={pricing['margin']:.1%}")

        if not dry_run:
            ok = update_ebay_price(oauth, sku, new_price, expected_listing_id=p.get('listing_id'))
            if ok:
                verified, live_price = verify_ebay_price_update(
                    oauth,
                    sku,
                    new_price,
                    expected_listing_id=p.get('listing_id'),
                )
                result['verified_price'] = live_price
                if verified:
                    result['status'] = 'updated'
                    try:
                        db_path = PROJECT_ROOT / "ebay_collection.db"
                        conn = sqlite3.connect(str(db_path))
                        cost_data = p['cost_breakdown']
                        cost_data['selling_price'] = new_price
                        cost_data['market_avg_price'] = market_avg
                        cost_data['pricing_strategy'] = strategy
                        cost_data['repriced_at'] = datetime.now().isoformat()
                        conn.execute(
                            "UPDATE collected_products SET cost_breakdown = ?, suggested_price = ? WHERE sku = ?",
                            (json.dumps(cost_data), new_price, sku)
                        )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        log.warning(f"  DB update failed: {e}")
                else:
                    result['status'] = 'verification_failed'
                    result['error'] = (
                        f"live offer price remained ${live_price:.2f}" if live_price is not None
                        else "unable to verify live offer price"
                    )
                    errors += 1
                    log.error(f"  {sku}: price update not verified on eBay ({result['error']})")
            else:
                result['status'] = 'error'
                errors += 1
            # Rate limit
            time.sleep(1.5)
        else:
            result['status'] = 'dry_run'

        results.append(result)

        # Progress checkpoint
        if processed % 50 == 0:
            log.info(f"  --- Progress: {processed}/{total} ---")

    # Phase 3: Summary
    no_change = sum(1 for r in results if r['status'] == 'no_change')

    log.info("\n" + "=" * 70)
    log.info("REPRICE SUMMARY")
    log.info("=" * 70)
    log.info(f"Total products:     {total}")
    log.info(f"Price changes:      {price_changes} (↑{price_up} / ↓{price_down})")
    log.info(f"No change needed:   {no_change}")
    if SALES_COOLDOWN_DAYS > 0:
        log.info(f"Held (recent sale): {held_recent_sale} "
                 f"(sold within {SALES_COOLDOWN_DAYS}d, mode={SALES_COOLDOWN_MODE})")
    log.info(f"No market data:     {no_market} (used STANDARD 15% margin)")
    if not dry_run:
        updated = sum(1 for r in results if r['status'] == 'updated')
        log.info(f"Successfully updated: {updated}")
        log.info(f"Errors:             {errors}")

    # Strategy distribution
    strategy_counts = defaultdict(int)
    source_counts = defaultdict(int)
    for r in results:
        strategy_counts[r['strategy']] += 1
        source_counts[r.get('market_source') or 'UNKNOWN'] += 1
    log.info(f"\nStrategy distribution:")
    for s, c in sorted(strategy_counts.items()):
        log.info(f"  {s}: {c}")
    log.info(f"\nMarket source distribution:")
    for source, count in sorted(source_counts.items()):
        log.info(f"  {source}: {count}")

    # Save detailed results
    timestamp_label = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = PROJECT_ROOT / 'reports' / f'reprice_report_{timestamp_label}.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    changes_csv_path, changes_html_path = (None, None)
    if not dry_run:
        changes_csv_path, changes_html_path = _write_change_reports(results, timestamp_label)
    report_timestamp = datetime.now().isoformat()
    changed_rows = _build_changed_rows(results)
    report_payload = {
        'status': 'ok',
        'timestamp': report_timestamp,
        'mode': 'dry_run' if dry_run else 'apply',
        'market_mode': market_client.market_mode,
        'marketplace_insights_access': insights_probe,
        'path': str(report_path),
        'change_reports': {
            'csv': str(changes_csv_path) if changes_csv_path else None,
            'html': str(changes_html_path) if changes_html_path else None,
        },
        'summary': {
            'total': total,
            'price_changes': price_changes,
            'price_up': price_up,
            'price_down': price_down,
            'no_change': no_change,
            'held_recent_sale': held_recent_sale,
            'no_market_data': no_market,
            'errors': errors,
            'strategies': dict(strategy_counts),
            'market_sources': dict(source_counts),
        },
        'cooldown': cooldown_meta,
        'market_research': market_cache,
        'results': results,
        'changed_rows': changed_rows,
    }
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report_payload, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"\nReport saved: {report_path}")
    if changes_csv_path:
        log.info(f"Changed SKU CSV: {changes_csv_path}")
    if changes_html_path:
        log.info(f"Changed SKU HTML: {changes_html_path}")

    # Print top price changes
    changed = [r for r in results if r['status'] != 'no_change' and r['current_price']]
    if changed:
        log.info(f"\nTop price decreases:")
        down_sorted = sorted([r for r in changed if r['price_diff'] < 0], key=lambda x: x['price_diff'])
        for r in down_sorted[:10]:
            log.info(f"  {r['sku']}: ${r['current_price']:.2f} → ${r['new_price']:.2f} "
                     f"({r['pct_change']:+.1f}%) [{r['strategy']}]")

        log.info(f"\nTop price increases:")
        up_sorted = sorted([r for r in changed if r['price_diff'] > 0], key=lambda x: -x['price_diff'])
        for r in up_sorted[:10]:
            log.info(f"  {r['sku']}: ${r['current_price']:.2f} → ${r['new_price']:.2f} "
                     f"({r['pct_change']:+.1f}%) [{r['strategy']}]")

    # Email report
    if send_email and not dry_run:
        _send_reprice_email(results, price_changes, price_up, price_down, errors,
                           no_market, strategy_counts, total,
                           report_path=report_path,
                           changes_csv_path=changes_csv_path,
                           changes_html_path=changes_html_path,
                           dedupe=True)

    # Mark CRO queue items done (only successfully updated SKUs)
    if from_cro_queue and not dry_run and cro_queue_skus:
        try:
            from src.services.cro_action_queue import mark_done
            done_skus = [r['sku'] for r in results
                         if r.get('sku') in cro_queue_skus and r.get('status') == 'updated']
            if done_skus:
                n = mark_done(done_skus, action='price_drop')
                log.info(f"CRO queue: marked {n} SKUs as done")
        except Exception as e:
            log.warning(f"CRO queue mark_done failed: {e}")

    return report_payload


def _send_reprice_email(results, n_changes, n_up, n_down, n_errors,
                        n_no_market, strategies, total, report_path=None,
                        changes_csv_path=None, changes_html_path=None,
                        dedupe: bool = False):
    """发送重新定价报告邮件"""
    claimed = False
    try:
        from src.utils.email_sender import send_email

        if dedupe:
            claimed = claim_reprice_email_delivery()
            if not claimed:
                log.info("📧 Smart Reprice summary already sent today; suppressing duplicate email")
                return False

        subject = f"📊 eBay Smart Reprice: {n_changes} price changes ({n_up}↑ / {n_down}↓)"

        body = f"""
<h2>eBay Smart Reprice Report</h2>
<p>Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<table border="1" cellpadding="5" style="border-collapse:collapse">
<tr><td>Total Products</td><td><b>{total}</b></td></tr>
<tr><td>Price Changes</td><td><b>{n_changes}</b> (↑{n_up} / ↓{n_down})</td></tr>
<tr><td>No Market Data</td><td>{n_no_market}</td></tr>
<tr><td>Errors</td><td>{n_errors}</td></tr>
</table>

<h3>Strategy Distribution</h3>
<ul>
{''.join(f'<li><b>{s}</b>: {c}</li>' for s, c in sorted(strategies.items()))}
</ul>

<p>
JSON Report: <code>{report_path or ''}</code><br>
Changed SKU CSV: <code>{changes_csv_path or ''}</code><br>
Changed SKU HTML: <code>{changes_html_path or ''}</code>
</p>

<h3>Changed SKUs</h3>
<table border="1" cellpadding="4" style="border-collapse:collapse;width:100%;font-size:13px">
<tr><th>Product</th><th>Old Price</th><th>New Price</th><th>Change</th><th>Margin</th><th>Strategy</th><th>Reason</th></tr>
"""
        changed = sorted([r for r in results if r['status'] == 'updated' and r['current_price']],
                        key=lambda x: abs(x['price_diff']), reverse=True)
        for r in changed:
            color = 'green' if r['price_diff'] > 0 else 'red'
            thumb_html = build_thumbnail_img_html(r.get('image_url', ''), width=54, height=54)
            sku = html_escape(str(r.get('sku', '') or ''))
            title = html_escape(str(r.get('title', '') or ''))
            strategy = html_escape(str(r.get('strategy', '') or ''))
            reason = html_escape(str(r.get('price_reason', '') or ''))
            body += (
                "<tr>"
                "<td style='vertical-align:top;min-width:210px'>"
                f"<div style='display:flex;gap:8px;align-items:flex-start'>{thumb_html}"
                f"<div><b>{sku}</b><br><span style='color:#666;font-size:12px'>{title}</span></div></div>"
                "</td>"
                f"<td style='vertical-align:top'>{_format_money(r.get('current_price'))}</td>"
                f"<td style='vertical-align:top'>{_format_money(r.get('new_price'))}</td>"
                f"<td style='vertical-align:top;color:{color}'>{float(r.get('pct_change') or 0):+.1f}%</td>"
                f"<td style='vertical-align:top'>{_format_margin_pct(r.get('margin'))}</td>"
                f"<td style='vertical-align:top'>{strategy}</td>"
                f"<td style='vertical-align:top;max-width:420px'>{reason}</td>"
                "</tr>\n"
            )
        body += "</table>"

        attachments = [
            str(path) for path in (report_path, changes_csv_path, changes_html_path)
            if path
        ]
        delivered = bool(send_email(subject, body, attachments=attachments))
        if delivered:
            if claimed:
                mark_reprice_email_delivery(delivered=True)
            log.info("📧 Reprice report email sent")
        else:
            if claimed:
                mark_reprice_email_delivery(delivered=False, error='SMTP delivery returned false')
            log.warning("📧 Reprice report email saved locally but SMTP delivery failed")
        return delivered
    except Exception as e:
        if claimed:
            try:
                mark_reprice_email_delivery(delivered=False, error=str(e))
            except Exception:
                pass
        log.warning(f"Failed to send email: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch Smart Reprice with Terapeak market research")
    parser.add_argument("--apply", action="store_true", help="Actually apply price changes (default: dry run)")
    parser.add_argument("--email", action="store_true", help="Send email report after apply")
    parser.add_argument(
        "--market-mode",
        choices=["auto", "sold_only", "browse_only"],
        help="Market research mode: auto (sold-data preferred), sold_only, or browse_only",
    )
    parser.add_argument("--from-cro-queue", action="store_true",
                        help="Only reprice SKUs in logs/cro_action_queue.jsonl pending P1 price_drop list")
    args = parser.parse_args()

    run_batch_reprice(
        dry_run=not args.apply,
        send_email=args.email,
        market_mode=args.market_mode,
        from_cro_queue=args.from_cro_queue,
    )
