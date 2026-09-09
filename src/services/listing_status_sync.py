"""
eBay 在售链接状态同步服务

功能：
1. 调用 eBay GetMyeBaySelling API 获取真实在售链接
2. 与本地数据库 PUBLISHED 状态对比
3. 自动将 eBay 上已结束/下架的链接在本地标记为 ENDED
4. 返回准确的在售链接数据

缓存机制：同步结果缓存到文件，避免频繁调用 eBay API
"""
import os
import sys
import json
import time
import logging
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

# 缓存文件路径和TTL
SYNC_CACHE_FILE = PROJECT_ROOT / 'logs' / '_listing_sync_cache.json'
SYNC_CACHE_TTL_HOURS = 2


def _get_trading_client():
    """创建 Trading API 客户端"""
    from src.services.ebay_auth import EbayOAuthService
    from src.clients.ebay_client import EbayClient
    from src.clients.ebay_trading_client import EbayTradingClient
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))

    if not oauth.is_authorized():
        raise RuntimeError("eBay not authorized — cannot sync listing status")

    # 主动获取有效token（确保不过期）
    oauth.get_valid_token()

    ebay = EbayClient(
        os.getenv("EBAY_APP_ID"),
        os.getenv("EBAY_CERT_ID"),
        os.getenv("EBAY_DEV_ID"),
        env=os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").lower(),
    )
    return EbayTradingClient(ebay)


def fetch_all_active_listing_ids(trading=None, max_pages=20) -> tuple[set, int, bool]:
    """
    获取 eBay 上所有在售的 listing ID 集合

    调用 GetMyeBaySelling + ActiveList，分页获取全部。
    注意：不按库存过滤，因为 eBay "Out of Stock" 功能会让零库存 listing
    仍显示为 Active（Seller Hub 里也算在 Active 总数中）。因此本函数返回的
    active_ids 会包含"已开启缺货控制的零库存 listing"，调用方据此判断死链时
    不会误伤缺货商品。

    Returns:
        (active_ids: set of listing_id strings,
         api_total: int — eBay API 报告的 TotalNumberOfEntries,
         fetch_complete: bool — 仅当分页被完整抓取时为 True（未遇失败页、
           未撞 max_pages 上限）。调用方必须在 fetch_complete=False 时放弃
           "死链"协调，否则会把没抓到的在售 listing 误判为已结束。)
    """
    if trading is None:
        trading = _get_trading_client()

    active_ids = set()
    api_total = 0
    per_page = 200
    ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
    fetch_complete = False  # 只有确认翻到最后一页/无更多条目才置 True

    for page in range(1, max_pages + 1):
        xml_payload = f"""
        <ActiveList>
            <Include>true</Include>
            <Pagination>
                <EntriesPerPage>{per_page}</EntriesPerPage>
                <PageNumber>{page}</PageNumber>
            </Pagination>
        </ActiveList>
        <DetailLevel>ReturnAll</DetailLevel>
        """

        # Retry with backoff
        response_text = None
        for attempt in range(3):
            try:
                response_text = trading.call("GetMyeBaySelling", xml_payload)
                break
            except Exception as e:
                logger.warning(f"GetMyeBaySelling page {page} attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))

        if not response_text:
            logger.error(f"Skipping page {page} after 3 retries")
            fetch_complete = False  # 抓取失败 → 快照残缺，禁止据此判死链
            break

        root = ET.fromstring(response_text)

        # 首页：获取 API 报告的总数
        if page == 1:
            total_elem = root.find(
                './/ebay:ActiveList/ebay:PaginationResult/ebay:TotalNumberOfEntries', ns
            )
            if total_elem is not None and total_elem.text:
                api_total = int(total_elem.text)
                logger.info(f"  eBay API TotalNumberOfEntries: {api_total}")

        # 关键修复：只匹配 ActiveList/ItemArray/Item，不匹配嵌套子元素
        items = root.findall('.//ebay:ActiveList/ebay:ItemArray/ebay:Item', ns)

        if not items:
            fetch_complete = True  # 无更多条目 → 抓完
            break

        for item in items:
            item_id_elem = item.find('ebay:ItemID', ns)
            if item_id_elem is not None and item_id_elem.text:
                active_ids.add(item_id_elem.text)

        # 检查是否还有更多页
        total_pages_elem = root.find(
            './/ebay:ActiveList/ebay:PaginationResult/ebay:TotalNumberOfPages', ns
        )
        if total_pages_elem is not None:
            total_pages = int(total_pages_elem.text)
            if page >= total_pages:
                fetch_complete = True  # 翻到最后一页 → 抓完
                break

        logger.info(f"  Page {page}: {len(items)} items, cumulative {len(active_ids)}")
        time.sleep(0.5)  # Rate limit courtesy
    else:
        # for 正常耗尽 range 而未 break → 撞上 max_pages 上限, 可能还有更多页
        fetch_complete = False

    logger.info(f"Total active listing IDs from API: {len(active_ids)} "
                f"(API reported: {api_total}, complete={fetch_complete})")
    return active_ids, api_total, fetch_complete


def get_db_published_listings() -> list:
    """
    获取本地数据库中所有 PUBLISHED 状态的 SKU → listing_id 映射

    Returns:
        list of dicts: [{'sku': ..., 'listing_id': ...}, ...]
    """
    db_path = str(PROJECT_ROOT / 'ebay_collection.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sku, listing_id FROM collected_products WHERE status = 'PUBLISHED'"
    ).fetchall()
    conn.close()
    return [{'sku': r['sku'], 'listing_id': r['listing_id']} for r in rows]


def _get_db_listings_with_listing_id(statuses: tuple[str, ...]) -> list:
    """返回给定状态、且挂着非空 listing_id 的 SKU → listing_id 映射。"""
    if not statuses:
        return []
    db_path = str(PROJECT_ROOT / 'ebay_collection.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT sku, listing_id FROM collected_products "
        f"WHERE status IN ({placeholders}) "
        f"AND listing_id IS NOT NULL AND listing_id != ''",
        tuple(statuses),
    ).fetchall()
    conn.close()
    return [{'sku': r['sku'], 'listing_id': r['listing_id']} for r in rows]


def sync_listing_status(force_refresh=False) -> dict:
    """
    同步 eBay 在售链接状态到本地数据库

    1. 获取 eBay 上真正在售的 listing ID
    2. 对比本地 PUBLISHED 的 listing
    3. 把 eBay 上已不存在/已结束的 listing 标记为 ENDED

    Returns:
        {
            'ebay_active_count': int,       # eBay 上真实在售数
            'db_published_count': int,      # 本地 PUBLISHED 数
            'stale_ended': [sku, ...],      # 本次被标记为 ENDED 的 SKU
            'synced_at': str,               # 同步时间
        }
    """
    # 检查缓存
    if not force_refresh:
        cached = _load_sync_cache()
        if cached:
            return cached

    logger.info("="*50)
    logger.info("Syncing listing status with eBay...")
    logger.info("="*50)

    try:
        trading = _get_trading_client()
    except Exception as e:
        logger.error(f"Cannot create Trading client: {e}")
        return {
            'ebay_active_count': -1,
            'db_published_count': -1,
            'stale_ended': [],
            'synced_at': datetime.now().isoformat(),
            'error': str(e),
        }

    # 1. 获取 eBay 真实在售
    active_ids, api_total, fetch_complete = fetch_all_active_listing_ids(trading)

    # 使用 API 报告的 TotalNumberOfEntries 作为权威数据
    # （比我们自己计数更准确，因为 eBay 内部可能有我们看不到的过滤）
    ebay_active_count = api_total if api_total > 0 else len(active_ids)

    # 完整性闸: 只有确认抓取完整(未撞 max_pages 上限、无失败页, 且抓到数不少于
    # API 报告总数)才允许据"不在 active_ids"判死链。否则残缺快照会把在售
    # listing 误判为已结束/误清 listing_id — 直接放弃本轮协调, 不动任何数据。
    fetch_looks_complete = fetch_complete and (api_total <= 0 or len(active_ids) >= api_total)
    if not fetch_looks_complete:
        logger.warning(
            "ActiveList 抓取不完整 (complete=%s, fetched=%d, api_total=%d) — "
            "跳过死链协调以免误伤在售 listing",
            fetch_complete, len(active_ids), api_total,
        )
        return {
            'ebay_active_count': ebay_active_count,
            'ebay_fetched_ids': len(active_ids),
            'db_published_count': -1,
            'db_active_after_sync': -1,
            'stale_ended': [],
            'stale_ready_cleared': [],
            'skipped_incomplete': True,
            'synced_at': datetime.now().isoformat(),
            'error': 'incomplete_active_list_fetch',
        }

    # 2. 获取本地 PUBLISHED
    db_listings = get_db_published_listings()
    db_published_count = len(db_listings)

    # 3. 找出本地 PUBLISHED 但 eBay 上已不在售的 → 标记 ENDED
    stale_skus = []
    for item in db_listings:
        lid = item['listing_id']
        # 跳过 DRAFT offers 和空 listing ID
        if not lid or lid.startswith('DRAFT-'):
            continue
        if lid not in active_ids:
            stale_skus.append(item['sku'])

    # 3b. 找出 READY/READY_TO_PUBLISH 但挂着已失效 listing_id 的行:
    #     MI 重识别的"曾刊登 → 未出单 → 死链 → 下架"候选。stale listing_id 会让
    #     batch_publish 复用死链而非重发, 并让状态看似"在售"而卡死。清掉 listing_id
    #     → 变回干净草稿, batch_publish 会当全新 listing 重发。
    #     缺货 listing 因开启 OOS 控制仍在 active_ids 里, 不会被清 (见 fetch 注释)。
    ready_listings = _get_db_listings_with_listing_id(('READY', 'READY_TO_PUBLISH'))
    stale_ready_cleared = [
        item['sku'] for item in ready_listings
        if item['listing_id']
        and not item['listing_id'].startswith('DRAFT-')
        and item['listing_id'] not in active_ids
    ]

    # 4. 批量更新本地数据库
    if stale_skus or stale_ready_cleared:
        if stale_skus:
            logger.info(f"Found {len(stale_skus)} stale listings (PUBLISHED in DB but not active on eBay)")
        if stale_ready_cleared:
            logger.info(f"Found {len(stale_ready_cleared)} READY drafts with dead listing_id — clearing for relist")
        db_path = str(PROJECT_ROOT / 'ebay_collection.db')
        conn = sqlite3.connect(db_path)
        for sku in stale_skus:
            conn.execute(
                "UPDATE collected_products SET status = 'ENDED' WHERE sku = ? AND status = 'PUBLISHED'",
                (sku,)
            )
            logger.info(f"  {sku} → ENDED (no longer active on eBay)")
        for sku in stale_ready_cleared:
            conn.execute(
                "UPDATE collected_products SET listing_id = NULL "
                "WHERE sku = ? AND status IN ('READY', 'READY_TO_PUBLISH')",
                (sku,)
            )
            logger.info(f"  {sku} → cleared stale listing_id (READY draft, listing dead) — will relist fresh")
        conn.commit()
        conn.close()

    result = {
        'ebay_active_count': ebay_active_count,  # eBay API 报告的真实在售数
        'ebay_fetched_ids': len(active_ids),     # 我们实际拉取到的 ItemID 数
        'db_published_count': db_published_count,
        'db_active_after_sync': db_published_count - len(stale_skus),
        'stale_ended': stale_skus,
        'stale_ready_cleared': stale_ready_cleared,
        'synced_at': datetime.now().isoformat(),
    }

    # 保存缓存
    _save_sync_cache(result)

    logger.info(f"Sync complete: eBay active={ebay_active_count} (fetched {len(active_ids)} IDs), "
                f"DB published={db_published_count}, ended={len(stale_skus)})")

    return result


def get_active_listing_count(force_refresh=False) -> int:
    """
    获取准确的在售链接数量

    优先使用缓存，超期时自动同步。
    用于广告监控和竞争监控 Dashboard。
    """
    result = sync_listing_status(force_refresh=force_refresh)
    if result.get('error'):
        # 同步失败，回退到 DB 查询
        db_path = str(PROJECT_ROOT / 'ebay_collection.db')
        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM collected_products WHERE status = 'PUBLISHED'"
        ).fetchone()[0]
        conn.close()
        return count
    return result.get('ebay_active_count', 0)


# ─── 缓存管理 ───

def _save_sync_cache(data: dict):
    """保存同步缓存"""
    try:
        SYNC_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SYNC_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save sync cache: {e}")


def _load_sync_cache(max_age_hours=SYNC_CACHE_TTL_HOURS) -> dict | None:
    """加载同步缓存（如果在有效期内）"""
    try:
        if not SYNC_CACHE_FILE.exists():
            return None
        with open(SYNC_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        synced_at = data.get('synced_at', '')
        if synced_at:
            dt = datetime.fromisoformat(synced_at)
            if datetime.now() - dt < timedelta(hours=max_age_hours):
                return data
        return None
    except Exception:
        return None


# ─── CLI ───

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    result = sync_listing_status(force_refresh=True)
    print(json.dumps(result, indent=2, ensure_ascii=False))
