"""
每日自动标题优化脚本

此脚本由 Windows 任务计划程序调用，自动优化 eBay 在售商品标题。

用法:
    python daily_optimize.py --batch-size 50
"""
import sys
import os
import io

# Ensure UTF-8 output (avoid emoji crashes on Chinese Windows scheduled tasks)
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if not _stream:
        continue
    try:
        encoding = (_stream.encoding or "").lower()
        if hasattr(_stream, "reconfigure"):
            if encoding != "utf-8":
                _stream.reconfigure(encoding="utf-8", errors="replace")
        elif hasattr(_stream, "buffer") and encoding != "utf-8":
            setattr(
                sys,
                _stream_name,
                io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
            )
    except Exception:
        pass
import argparse
from pathlib import Path
from datetime import datetime

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# 确保工作目录为项目根目录（计划任务可能在其他目录启动）
os.chdir(PROJECT_ROOT)

import sqlite3
import json
import logging
from src.utils.title_sanitizer import normalize_listing_title_for_ebay

# 设置日志
log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / f"daily_optimize_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def scheduled_title_optimization_disabled():
    """Return True unless scheduled title optimization is explicitly re-enabled."""
    raw = os.getenv("ENABLE_SCHEDULED_TITLE_OPTIMIZATION", "")
    return raw.strip().lower() not in {"1", "true", "yes", "on"}


def get_trading_client():
    """获取 eBay Trading API Client"""
    from src.services.ebay_auth import EbayOAuthService
    from src.clients.ebay_client import EbayClient
    from src.clients.ebay_trading_client import EbayTradingClient
    
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    
    if not oauth.is_authorized():
        logger.error("eBay 未授权!")
        return None, None
    
    # 主动获取有效token（自动刷新过期token）
    try:
        token = oauth.get_valid_token()
        logger.info(f"Token 有效，长度: {len(token)}")
    except Exception as e:
        logger.error(f"Token 刷新失败: {e}")
        return None, None
    
    ebay = EbayClient(
        os.getenv("EBAY_APP_ID"),
        os.getenv("EBAY_CERT_ID"),
        os.getenv("EBAY_DEV_ID"),
        env="production"
    )
    trading = EbayTradingClient(ebay)
    
    return oauth, trading


def fetch_active_listings(trading, limit=50, max_pages=10):
    """获取在售且有库存的商品列表（过滤已结束和零库存的listing）"""
    import xml.etree.ElementTree as ET
    
    listings = []
    skipped_zero_qty = 0
    per_page = 200  # eBay max: 200 per page, always fetch max for completeness
    
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
        
        try:
            response = trading.call("GetMyeBaySelling", xml_payload)
            root = ET.fromstring(response)
        except Exception as e:
            logger.warning(f"获取第 {page} 页失败: {e}")
            break
        
        ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
        items = root.findall('.//ebay:ActiveList/ebay:ItemArray/ebay:Item', ns)
        
        if not items:
            break
        
        for item in items:
            item_id = item.find('ebay:ItemID', ns)
            title = item.find('ebay:Title', ns)
            category = item.find('ebay:PrimaryCategory/ebay:CategoryID', ns)
            
            # 提取库存数量 — 跳过零库存 listing
            qty_available = item.find('.//ebay:QuantityAvailable', ns)
            quantity = item.find('ebay:Quantity', ns)
            selling_status = item.find('ebay:SellingStatus/ebay:ListingStatus', ns)
            
            qty = 0
            if qty_available is not None and qty_available.text:
                try:
                    qty = int(qty_available.text)
                except ValueError:
                    qty = 0
            elif quantity is not None and quantity.text:
                try:
                    qty = int(quantity.text)
                except ValueError:
                    qty = 0
            
            # 跳过已结束或零库存的 listing
            if selling_status is not None and selling_status.text in ('Ended', 'Completed'):
                skipped_zero_qty += 1
                continue
            if qty <= 0:
                skipped_zero_qty += 1
                continue
            
            if item_id is not None and title is not None:
                listings.append({
                    "ItemID": item_id.text,
                    "Title": title.text,
                    "CategoryID": category.text if category is not None else "",
                    "Quantity": qty
                })
        
        # 检查是否还有更多页
        total_pages_elem = root.find('.//ebay:ActiveList/ebay:PaginationResult/ebay:TotalNumberOfPages', ns)
        if total_pages_elem is not None:
            total_pages = int(total_pages_elem.text)
            if page >= total_pages:
                break
        
        logger.info(f"  第 {page} 页: 累计 {len(listings)} 个在售商品")
    
    if skipped_zero_qty > 0:
        logger.info(f"  已跳过 {skipped_zero_qty} 个零库存/已结束的 listing")
    
    return listings


def fetch_item_description(trading, item_id):
    """获取商品描述"""
    import xml.etree.ElementTree as ET
    import html
    import re
    
    try:
        xml_payload = f"""
        <ItemID>{item_id}</ItemID>
        <DetailLevel>ReturnAll</DetailLevel>
        """
        
        response = trading.call("GetItem", xml_payload)
        root = ET.fromstring(response)
        
        ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
        desc_elem = root.find('.//ebay:Item/ebay:Description', ns)
        
        if desc_elem is not None and desc_elem.text:
            desc_html = desc_elem.text
            desc_text = re.sub(r'<[^>]+>', ' ', desc_html)
            desc_text = html.unescape(desc_text)
            desc_text = re.sub(r'\s+', ' ', desc_text).strip()
            return desc_text[:1000]
    except Exception:
        pass
    return ""


def _get_sku_for_listing(item_id):
    """从数据库查找 listing_id 对应的 SKU"""
    db_path = str(PROJECT_ROOT / "ebay_collection.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT sku FROM collected_products WHERE listing_id = ?", (str(item_id),))
    row = cur.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]

    # Fallback: ask Trading API for the listing SKU and backfill the local mapping.
    try:
        import xml.etree.ElementTree as ET

        _, trading = get_trading_client()
        if trading:
            xml_payload = f"""
            <ItemID>{item_id}</ItemID>
            <DetailLevel>ReturnAll</DetailLevel>
            """
            response = trading.call("GetItem", xml_payload)
            root = ET.fromstring(response)
            ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
            sku_elem = root.find('.//ebay:Item/ebay:SKU', ns)
            sku = sku_elem.text.strip() if sku_elem is not None and sku_elem.text else None
            if sku:
                cur.execute(
                    "UPDATE collected_products SET listing_id = ? WHERE sku = ? AND (listing_id IS NULL OR listing_id = '')",
                    (str(item_id), sku),
                )
                conn.commit()
                conn.close()
                return sku
    except Exception as e:
        logger.warning(f"    Trading API fallback failed for listing {item_id}: {e}")

    conn.close()
    return None


def _update_title_via_inventory_api(item_id, new_title):
    """
    通过 Inventory API 更新标题（用于 inventory-managed items）
    这些 listing 不支持 Trading API 的 ReviseItem
    """
    import requests
    from src.services.ebay_auth import EbayOAuthService
    
    logger = logging.getLogger(__name__)
    
    sku = _get_sku_for_listing(item_id)
    if not sku:
        logger.warning(f"    无法找到 listing {item_id} 对应的 SKU")
        return False
    
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    
    base_url = "https://api.ebay.com"
    
    # 1. 先获取当前 inventory item
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    
    get_url = f"{base_url}/sell/inventory/v1/inventory_item/{sku}"
    resp = requests.get(get_url, headers=headers, timeout=30, verify=False)
    
    if resp.status_code != 200:
        logger.warning(f"    获取 inventory item 失败: {resp.status_code}")
        return False
    
    item_data = resp.json()
    
    # 2. 清理 payload（移除 GET 返回的只读字段 + 修复无效值）
    for key in ('sku', 'locale'):
        item_data.pop(key, None)
    
    # 修复 weight=0 问题（eBay 不接受 weight.value=0）
    pws = item_data.get('packageWeightAndSize', {})
    w = pws.get('weight', {})
    if w.get('value', 0) <= 0:
        item_data.pop('packageWeightAndSize', None)
    
    # 移除 availability 中的只读字段
    avail = item_data.get('availability', {})
    ship = avail.get('shipToLocationAvailability', {})
    ship.pop('allocationByFormat', None)
    
    # 3. 更新标题
    if "product" not in item_data:
        item_data["product"] = {}
    safe_title, _ = normalize_listing_title_for_ebay(new_title, source_title=current_title or new_title)
    item_data["product"]["title"] = safe_title
    
    # 3. PUT 更新 inventory item
    put_resp = requests.put(get_url, headers=headers, json=item_data, timeout=60, verify=False)
    
    if put_resp.status_code not in (200, 204):
        logger.warning(f"    Inventory API 更新失败: {put_resp.status_code} - {put_resp.text[:200]}")
        return False
    
    # 4. 查找该 SKU 的 offer 并重新发布，让标题变更生效到 live listing
    import urllib.parse
    offer_url = f"{base_url}/sell/inventory/v1/offer?sku={urllib.parse.quote(sku, safe='')}"
    offer_resp = requests.get(offer_url, headers=headers, timeout=30, verify=False)
    
    if offer_resp.status_code != 200:
        logger.warning(f"    获取 offer 失败: {offer_resp.status_code}")
        return False
    
    offers = offer_resp.json().get("offers", [])
    if not offers:
        logger.warning(f"    SKU {sku} 没有找到 offer")
        return False
    
    offer_id = offers[0]["offerId"]
    
    # 5. 重新发布 offer
    pub_url = f"{base_url}/sell/inventory/v1/offer/{offer_id}/publish"
    pub_resp = requests.post(pub_url, headers=headers, timeout=60, verify=False)
    
    if pub_resp.status_code == 200:
        logger.info(f"    ✅ 通过 Inventory API 更新成功 (SKU: {sku}, Offer: {offer_id})")
        return True
    else:
        logger.warning(f"    Offer 重新发布失败: {pub_resp.status_code} - {pub_resp.text[:200]}")
        return False


def update_item_title(trading, item_id, new_title, max_retries=2):
    """
    更新商品标题（带重试、错误日志、Inventory API 回退）
    
    - 先尝试 Trading API ReviseItem
    - 如果返回 21919474（inventory item 不允许），自动回退到 Inventory API
    """
    import xml.etree.ElementTree as ET
    import time
    
    logger = logging.getLogger(__name__)
    
    xml_payload = f"""
    <Item>
        <ItemID>{item_id}</ItemID>
        <Title>{new_title}</Title>
    </Item>
    """
    
    for attempt in range(max_retries):
        try:
            response = trading.call("ReviseItem", xml_payload)
            root = ET.fromstring(response)
            
            ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
            ack = root.find('.//ebay:Ack', ns)
            
            if ack is not None and ack.text in ['Success', 'Warning']:
                return True
            
            # 提取 eBay 错误信息
            errors = root.findall('.//ebay:Errors', ns)
            is_inventory_item = False
            is_rate_limited = False
            is_ended = False
            
            for err in errors:
                code = err.find('ebay:ErrorCode', ns)
                msg = err.find('ebay:LongMessage', ns) or err.find('ebay:ShortMessage', ns)
                if code is not None:
                    error_code = code.text
                    error_text = msg.text[:200] if msg is not None and msg.text else 'Unknown'
                    
                    if error_code in ('21919474', '21919456'):
                        is_inventory_item = True
                    elif error_code in ('10007', '21916750', '932', '10001'):
                        is_rate_limited = True
                    elif error_code == '291':
                        # Auction ended — listing 已结束，跳过（不算失败）
                        is_ended = True
                        logger.info(f"    ⏭️ Listing {item_id} 已结束，跳过")
                    else:
                        logger.warning(f"    eBay Error [{error_code}]: {error_text}")
            
            # 已结束的 listing → 标记为已结束，返回 'ended' 特殊值
            if is_ended:
                return 'ended'
            
            # Inventory-managed item → 使用 Inventory API 更新
            if is_inventory_item:
                logger.info(f"    Inventory item，切换到 Inventory API...")
                return _update_title_via_inventory_api(item_id, new_title)
            
            # 限流 → 等待后重试
            if is_rate_limited and attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                logger.info(f"    API限流，等待 {wait}s 后重试...")
                time.sleep(wait)
                continue
            
            return False
            
        except Exception as e:
            logger.warning(f"    ReviseItem异常: {e}")
            if attempt < max_retries - 1:
                time.sleep(3)
                continue
            return False
    
    return False


def save_history(db_path, item_id, original, optimized, updated_to_ebay, round_id):
    """保存优化历史（包含轮回ID）"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 确保表存在
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS optimization_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            original_title TEXT NOT NULL,
            optimized_title TEXT,
            char_count INTEGER,
            status TEXT DEFAULT 'pending',
            updated_to_ebay INTEGER DEFAULT 0,
            round_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 添加 round_id 列（如果不存在）
    try:
        cursor.execute("ALTER TABLE optimization_history ADD COLUMN round_id INTEGER DEFAULT 0")
    except Exception:
        pass
    
    cursor.execute(
        """INSERT INTO optimization_history 
           (item_id, original_title, optimized_title, char_count, status, updated_to_ebay, round_id) 
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (item_id, original, optimized, len(optimized), 'completed', updated_to_ebay, round_id)
    )
    conn.commit()
    conn.close()


def get_current_round_and_optimized_ids(db_path):
    """获取当前轮回ID和已优化的商品ID"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 确保 pagination_state 表存在
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pagination_state (
            id INTEGER PRIMARY KEY,
            last_page INTEGER DEFAULT 1,
            last_item_index INTEGER DEFAULT 0,
            total_items INTEGER DEFAULT 0,
            current_round_id INTEGER DEFAULT 1,
            last_run_date TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO pagination_state (id, last_page, current_round_id) VALUES (1, 1, 1)")
    conn.commit()
    
    # 获取当前轮回 ID
    cursor.execute("SELECT current_round_id FROM pagination_state WHERE id = 1")
    row = cursor.fetchone()
    current_round = row[0] if row else 1
    
    # 获取当前轮回已优化的商品 ID
    cursor.execute(
        "SELECT DISTINCT item_id FROM optimization_history WHERE round_id = ?",
        (current_round,)
    )
    optimized_ids = {row[0] for row in cursor.fetchall()}
    
    conn.close()
    return current_round, optimized_ids


def update_pagination_state(db_path, item_count):
    """更新分页状态"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE pagination_state 
        SET last_item_index = last_item_index + ?,
            last_run_date = ?
        WHERE id = 1
    """, (item_count, datetime.now().strftime('%Y-%m-%d %H:%M')))
    conn.commit()
    conn.close()


def advance_to_next_round(db_path):
    """自动推进到下一轮优化"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE pagination_state 
        SET current_round_id = current_round_id + 1,
            last_item_index = 0,
            last_run_date = ?
        WHERE id = 1
    """, (datetime.now().strftime('%Y-%m-%d %H:%M'),))
    conn.commit()
    
    cursor.execute("SELECT current_round_id FROM pagination_state WHERE id = 1")
    new_round = cursor.fetchone()[0]
    conn.close()
    return new_round


def main():
    parser = argparse.ArgumentParser(description='每日自动标题优化')
    parser.add_argument('--batch-size', type=int, default=50, help='每次优化的商品数量')
    parser.add_argument('--auto-update', action='store_true', default=True, help='自动更新到 eBay')
    parser.add_argument('--email', action='store_true', help='发送邮件通知')
    args = parser.parse_args()

    if scheduled_title_optimization_disabled():
        logger.warning(
            "标题优化定时任务已停用: 跳过 daily_optimize.py。"
            " 如需临时恢复，设置 ENABLE_SCHEDULED_TITLE_OPTIMIZATION=1。"
        )
        return
    
    logger.info(f"="*50)
    logger.info(f"开始每日标题优化任务")
    logger.info(f"批量大小: {args.batch_size}")
    logger.info(f"自动更新: {args.auto_update}")
    logger.info(f"="*50)
    
    # 获取 Trading Client
    oauth, trading = get_trading_client()
    if not trading:
        logger.error("无法连接 eBay API")
        return
    
    # 获取优化器
    from qwen_optimizer import QwenOptimizer
    api_key = os.getenv("QWEN_API_KEY")
    if not api_key:
        logger.error("QWEN_API_KEY 未设置")
        return
    
    optimizer = QwenOptimizer(api_key=api_key)
    
    # 数据库路径
    db_path = Path(__file__).parent / "optimization_history.db"
    
    # 获取当前轮回和已优化的商品
    current_round, optimized_ids = get_current_round_and_optimized_ids(db_path)
    logger.info(f"当前优化轮回: 第 {current_round} 轮")
    logger.info(f"本轮已优化商品数: {len(optimized_ids)}")
    
    # 获取商品列表（多页获取以覆盖全部 listings）
    logger.info(f"获取在售商品...")
    all_listings = fetch_active_listings(trading, limit=args.batch_size, max_pages=10)
    
    # 过滤掉本轮已优化的商品
    listings = [item for item in all_listings if item['ItemID'] not in optimized_ids]
    listings = listings[:args.batch_size]  # 取指定数量
    
    logger.info(f"获取到 {len(all_listings)} 个商品，过滤后待优化 {len(listings)} 个")
    
    if not listings:
        # 自动推进到下一轮
        try:
            new_round = advance_to_next_round(db_path)
            logger.info(f"本轮所有商品已优化完成！自动推进到第 {new_round} 轮。")
        except Exception as e:
            logger.error(f"推进到下一轮失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return
        
        # 重新获取全部 listings，开始新一轮
        listings = all_listings[:args.batch_size]
        current_round = new_round
        optimized_ids = set()
        
        if not listings:
            logger.info("没有在售商品可优化。")
            return
        
        logger.info(f"第 {new_round} 轮开始，待优化 {len(listings)} 个商品")
    
    # 优化每个商品
    success_count = 0
    update_count = 0
    optimization_samples = []  # 记录优化样本用于邮件报告
    
    for idx, item in enumerate(listings):
        item_id = item['ItemID']
        original_title = item['Title']
        
        logger.info(f"[{idx+1}/{len(listings)}] 优化: {item_id}")
        
        try:
            # 获取描述
            description = fetch_item_description(trading, item_id)
            
            # 优化标题（会自动从 Terapeak/Browse API 获取市场数据）
            optimized = optimizer.optimize_title(
                original_title,
                category=item.get('CategoryID', ''),
                max_length=80,
                description=description
            )
            
            logger.info(f"  原始: {original_title}")
            logger.info(f"  优化: {optimized} ({len(optimized)} chars)")
            
            # 记录前10个样本到邮件
            if len(optimization_samples) < 10 and optimized != original_title:
                optimization_samples.append({
                    'item_id': item_id,
                    'original': original_title,
                    'optimized': optimized
                })
            
            # 更新到 eBay
            updated_to_ebay = 0
            if args.auto_update and optimized != original_title:
                import html
                import time
                safe_title = html.escape(optimized)
                result = update_item_title(trading, item_id, safe_title)
                if result == 'ended':
                    # Listing 已结束，跳过但不算失败
                    logger.info(f"  ⏭️ 已结束，跳过")
                    updated_to_ebay = -1  # 标记为已跳过
                elif result:
                    updated_to_ebay = 1
                    update_count += 1
                    logger.info(f"  ✅ 已更新到 eBay")
                else:
                    logger.warning(f"  ⚠️ 更新失败")
                
                # 每次 ReviseItem 调用后等待 2 秒，避免 eBay API 限流
                time.sleep(2)
            
            # 保存历史（包含轮回ID）
            save_history(db_path, item_id, original_title, optimized, updated_to_ebay, current_round)
            success_count += 1
            
        except Exception as e:
            logger.error(f"  ❌ 错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    # 更新分页状态
    update_pagination_state(db_path, success_count)
    
    logger.info(f"="*50)
    logger.info(f"优化完成!")
    logger.info(f"成功: {success_count}/{len(listings)}")
    logger.info(f"已更新到 eBay: {update_count}")
    logger.info(f"="*50)
    
    # 发送邮件通知
    if args.email:
        send_email_report(
            total_processed=len(listings),
            success_count=success_count,
            update_count=update_count,
            failed_count=len(listings) - success_count,
            current_round=current_round,
            total_in_round=len(optimized_ids) + success_count,
            total_listings=len(all_listings),
            samples=optimization_samples
        )


def send_email_report(total_processed: int, success_count: int, 
                      update_count: int, failed_count: int,
                      current_round: int = 0, total_in_round: int = 0,
                      total_listings: int = 0, samples: list = None):
    """发送优化报告邮件（含 Terapeak 市场数据参考 + 多提供商重试）"""
    from src.utils.email_sender import send_email
    
    from_email = os.getenv("NOTIFICATION_EMAIL")
    if not from_email:
        logger.warning("未配置邮件凭据 (NOTIFICATION_EMAIL)")
        return False
    
    try:
        # 构建优化样本表格
        samples_html = ""
        if samples:
            rows = ""
            for i, s in enumerate(samples, 1):
                rows += f"""
                <tr>
                    <td style="padding:6px 10px;border-bottom:1px solid #eee;color:#999;font-size:11px">{i}</td>
                    <td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:12px;color:#888;text-decoration:line-through">{s['original']}</td>
                    <td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:12px;color:#1a1a2e;font-weight:500">{s['optimized']}</td>
                </tr>"""
            samples_html = f"""
            <div style="margin-top:20px">
                <h3 style="color:#302b63;font-size:14px;margin-bottom:10px">🔍 优化样本（含 Terapeak 市场关键词参考）</h3>
                <table style="width:100%;border-collapse:collapse;font-size:12px">
                    <tr style="background:#f8f9fb">
                        <th style="padding:8px 10px;text-align:left;font-size:11px;color:#888">#</th>
                        <th style="padding:8px 10px;text-align:left;font-size:11px;color:#888">原标题</th>
                        <th style="padding:8px 10px;text-align:left;font-size:11px;color:#888">优化后（参考市场热词）</th>
                    </tr>
                    {rows}
                </table>
            </div>"""
        
        # 进度条
        progress_pct = round(total_in_round / total_listings * 100) if total_listings > 0 else 0

        from src.utils.store_profile import get_store_profile
        _brand = get_store_profile().brand_name
        _brand_upper = _brand.upper()
        html_content = f"""
        <html>
        <body style="font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;background:#f5f5f5">
            <div style="background:linear-gradient(135deg,#0f0c29,#302b63);color:white;padding:20px 24px;border-radius:10px 10px 0 0">
                <h2 style="margin:0;color:#d4af37;font-size:18px;letter-spacing:2px">{_brand_upper} TITLE OPTIMIZER</h2>
                <p style="color:#b8b8d1;font-size:12px;margin-top:4px">{datetime.now().strftime('%Y-%m-%d %H:%M')} · 第 {current_round} 轮 · Terapeak 市场数据驱动</p>
            </div>
            
            <div style="background:white;padding:20px;border-radius:0 0 10px 10px;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
                <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap">
                    <div style="flex:1;min-width:100px;background:#f8f9fb;padding:12px;border-radius:8px;text-align:center">
                        <div style="font-size:24px;font-weight:700;color:#1a1a2e">{total_processed}</div>
                        <div style="font-size:11px;color:#888">处理总数</div>
                    </div>
                    <div style="flex:1;min-width:100px;background:#eafaf1;padding:12px;border-radius:8px;text-align:center">
                        <div style="font-size:24px;font-weight:700;color:#00b894">{update_count}</div>
                        <div style="font-size:11px;color:#888">已更新 eBay</div>
                    </div>
                    <div style="flex:1;min-width:100px;background:#eef2ff;padding:12px;border-radius:8px;text-align:center">
                        <div style="font-size:24px;font-weight:700;color:#6c5ce7">{success_count}</div>
                        <div style="font-size:11px;color:#888">优化成功</div>
                    </div>
                    <div style="flex:1;min-width:100px;background:{'#fff0f0' if failed_count > 0 else '#f8f9fb'};padding:12px;border-radius:8px;text-align:center">
                        <div style="font-size:24px;font-weight:700;color:{'#e74c3c' if failed_count > 0 else '#ccc'}">{failed_count}</div>
                        <div style="font-size:11px;color:#888">失败</div>
                    </div>
                </div>
                
                <div style="margin-bottom:16px">
                    <div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:4px">
                        <span>第 {current_round} 轮进度</span>
                        <span>{total_in_round}/{total_listings} ({progress_pct}%)</span>
                    </div>
                    <div style="background:#e8e8e8;border-radius:4px;height:8px;overflow:hidden">
                        <div style="background:linear-gradient(90deg,#6c5ce7,#d4af37);width:{progress_pct}%;height:100%;border-radius:4px"></div>
                    </div>
                </div>
                
                {samples_html}
                
                <p style="color:#aaa;font-size:11px;margin-top:20px;text-align:center">
                    {_brand} Title Optimizer · Powered by Qwen AI + eBay Market Data
                </p>
            </div>
        </body>
        </html>
        """
        
        subject = f"📝 eBay 标题优化 - {datetime.now().strftime('%Y-%m-%d')} | {update_count}/{total_processed} 已更新 | 第{current_round}轮"
        return send_email(subject, html_content)
        
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"脚本异常退出: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        # 确保日志刷新到文件（计划任务可能提前终止）
        logging.shutdown()
