"""
库存与价格同步服务

每日检查大建云仓库存和价格，同步到 eBay:
1. 无库存 → eBay 库存设为 0
2. 价格变化 → 按利润率重新计算 eBay 售价
3. 已下架商品 → 跳过不处理
"""
import sys
import os
import json
import sqlite3
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")
from src.clients.dajian_client import extract_available_inventory_quantity


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _is_transient_dajian_connect_error(error: Exception) -> bool:
    message = str(error)
    transient_markers = (
        "NameResolutionError",
        "getaddrinfo failed",
        "Connection Error",
        "Max retries exceeded",
        "Read timed out",
        "Connection timed out",
        "API Timeout",
        "Proxy Error",
        "Temporary failure",
    )
    return any(marker in message for marker in transient_markers)


@dataclass
class SyncResult:
    """同步结果"""
    sku: str
    action: str  # 'out_of_stock', 'price_updated', 'skipped', 'error'
    old_value: str = ""
    new_value: str = ""
    message: str = ""
    supplier_in_stock: Optional[bool] = None


class InventorySyncService:
    """库存同步服务"""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(PROJECT_ROOT / "ebay_collection.db")
        self.logger = logging.getLogger(__name__)
        self._dajian_client = None
        self._price_cache: Dict[str, Dict] = {}
        self._inventory_cache: Dict[str, Dict] = {}
        self._last_dajian_connection_error = ""
        self.last_sync_scope_count = 0
        self.last_sync_skipped_count = 0
        self._init_sync_table()

    def _get_dajian_client(self):
        """Lazily create a single Dajian client so one sync run can reuse HTTP sessions."""
        if self._dajian_client is not None:
            return self._dajian_client

        from src.clients.dajian_client import DaJianClient

        client_id = os.getenv("DAJIAN_API_KEY")
        client_secret = os.getenv("DAJIAN_API_SECRET")

        if not client_id or not client_secret:
            raise ValueError("Missing DAJIAN_API_KEY or DAJIAN_API_SECRET in .env")

        self._dajian_client = DaJianClient(client_id, client_secret)
        return self._dajian_client

    @property
    def last_dajian_connection_error(self) -> str:
        return getattr(self, "_last_dajian_connection_error", "")

    def test_dajian_connection(self, attempts: Optional[int] = None, delay_sec: Optional[float] = None) -> bool:
        """Test Dajian connectivity using the shared client, with high-level retries."""
        attempts = attempts if attempts is not None else _env_int("DAJIAN_CONNECT_ATTEMPTS", 6)
        delay_sec = delay_sec if delay_sec is not None else _env_float("DAJIAN_CONNECT_DELAY_SEC", 60.0)
        last_error: Optional[Exception] = None
        self._last_dajian_connection_error = ""

        for attempt in range(1, attempts + 1):
            try:
                self._get_dajian_client().get_product_list(page=1, page_size=100)
                self._last_dajian_connection_error = ""
                return True
            except Exception as e:
                last_error = e
                self._last_dajian_connection_error = str(e)
                self.logger.warning(f"Dajian connection test failed (attempt {attempt}/{attempts}): {e}")
                if not _is_transient_dajian_connect_error(e):
                    self.logger.error(f"Dajian connection test stopped on non-network API error: {e}")
                    return False

            if attempt < attempts:
                self._dajian_client = None
                sleep_for = delay_sec * attempt
                self.logger.warning(f"Dajian connection retry scheduled in {sleep_for:.0f}s")
                time.sleep(sleep_for)

        if last_error is not None:
            self.logger.error(f"Dajian connection unavailable after {attempts} attempts. Last error: {last_error}")
        return False

    def _prefetch_dajian_data(self, skus: List[str], chunk_size: int = 200) -> None:
        """Warm price/inventory caches in batches to avoid per-SKU round trips."""
        if not skus:
            return

        self._price_cache = {}
        self._inventory_cache = {}
        client = self._get_dajian_client()
        unique_skus = list(dict.fromkeys(skus))
        price_hits = 0
        inventory_hits = 0

        for start in range(0, len(unique_skus), chunk_size):
            chunk = unique_skus[start:start + chunk_size]

            try:
                for row in client.get_product_prices(chunk):
                    sku = row.get('sku')
                    if sku:
                        self._price_cache[sku] = row
                        price_hits += 1
            except Exception as e:
                self.logger.warning(
                    f"预加载价格缓存失败 [{start + 1}-{start + len(chunk)}]: {str(e)[:120]}"
                )

            try:
                for row in client.get_inventory(chunk):
                    sku = row.get('sku')
                    if sku:
                        self._inventory_cache[sku] = row
                        inventory_hits += 1
            except Exception as e:
                self.logger.warning(
                    f"预加载库存缓存失败 [{start + 1}-{start + len(chunk)}]: {str(e)[:120]}"
                )

        self.logger.info(
            f"大建缓存预加载完成: 价格 {price_hits}/{len(unique_skus)}, 库存 {inventory_hits}/{len(unique_skus)}"
        )
    
    def _init_sync_table(self):
        """初始化同步记录表"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS inventory_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                action TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                message TEXT,
                synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sku, synced_at)
            )
        """)
        
        conn.commit()
        conn.close()
    
    def get_last_sync_time(self, sku: str) -> Optional[str]:
        """获取 SKU 上次同步时间"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT MAX(synced_at) FROM inventory_sync_log 
            WHERE sku = ?
        """, (sku,))
        
        result = cur.fetchone()
        conn.close()
        
        return result[0] if result and result[0] else None
    
    def record_sync(self, result: 'SyncResult'):
        """记录同步结果"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO inventory_sync_log (sku, action, old_value, new_value, message)
            VALUES (?, ?, ?, ?, ?)
        """, (result.sku, result.action, result.old_value, result.new_value, result.message))
        
        conn.commit()
        conn.close()
    
    def get_last_sync_action(self, sku: str) -> Optional[str]:
        """获取 SKU 上次同步的操作类型 (out_of_stock, price_updated, no_change, etc.)"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT action FROM inventory_sync_log 
            WHERE sku = ?
            ORDER BY synced_at DESC
            LIMIT 1
        """, (sku,))
        
        result = cur.fetchone()
        conn.close()
        
        return result[0] if result else None
    
    def _get_consecutive_skip_count(self, sku: str) -> int:
        """获取 SKU 连续 'skipped' (API 异常) 的天数"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT action FROM inventory_sync_log 
            WHERE sku = ?
            ORDER BY synced_at DESC
            LIMIT 5
        """, (sku,))
        
        rows = cur.fetchall()
        conn.close()
        
        count = 0
        for row in rows:
            if row[0] == 'skipped':
                count += 1
            else:
                break
        
        return count
    
    def update_product_cost_in_db(self, sku: str, new_price: float, 
                                   shipping_cost: float, new_selling_price: float):
        """更新数据库中的 cost_breakdown 和 suggested_price"""
        from src.services.pricing_engine import PricingEngine
        
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        # 重新计算完整成本分解
        is_oversize = True  # 大件商品默认超大
        cost_breakdown = PricingEngine.calculate_dajian_cost(
            new_price, shipping_cost, is_oversize=is_oversize
        )
        
        cur.execute("""
            UPDATE collected_products 
            SET cost_breakdown = ?, suggested_price = ?
            WHERE sku = ?
        """, (json.dumps(cost_breakdown), new_selling_price, sku))
        
        conn.commit()
        conn.close()
        self.logger.info(f"  已更新 {sku} 数据库: 成本={json.dumps(cost_breakdown)}, 售价=${new_selling_price:.2f}")

    def _get_total_cost_for_sku(self, sku: str) -> Optional[float]:
        """读取 SKU 的总到岸成本 (cost_breakdown.total_dajian_cost).

        被 update_ebay_price 守门员调用. 数据缺失返回 None, 不阻拦改价.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT cost_breakdown FROM collected_products WHERE sku = ?", (sku,))
            row = cur.fetchone()
            conn.close()
            if not row or not row[0]:
                return None
            cb = json.loads(row[0])
            tc = cb.get('total_dajian_cost')
            return float(tc) if tc and float(tc) > 0 else None
        except Exception:
            return None

    def get_skus_synced_today(self) -> set:
        """获取今日已同步的 SKU 列表"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        cur.execute("""
            SELECT DISTINCT sku FROM inventory_sync_log 
            WHERE date(synced_at) = ?
              AND action IN ('out_of_stock', 'price_updated', 'no_change', 'restocked', 'skipped')
        """, (today,))
        
        skus = {row[0] for row in cur.fetchall()}
        conn.close()
        
        return skus
        
    def get_published_products(self) -> List[Dict]:
        """获取所有已发布的产品"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        cur.execute("""
            SELECT sku, title, url, cost_breakdown, listing_id
            FROM collected_products 
            WHERE status = 'PUBLISHED'
        """)
        
        products = []
        for row in cur.fetchall():
            products.append({
                'sku': row['sku'],
                'title': row['title'],
                'source_url': row['url'],
                'cost_breakdown': json.loads(row['cost_breakdown']) if row['cost_breakdown'] else {},
                'ebay_listing_id': row['listing_id']
            })
        
        conn.close()
        return products
    
    def check_dajian_stock(self, sku: str) -> Tuple[Optional[bool], Optional[float], Optional[float], Optional[int]]:
        """
        检查大建云仓库存和价格
        
        使用新版 API 接口:
        - /buyer/inventory/quantity/v2 获取库存
        - /buyer/product/price/v1 获取价格
        
        Returns:
            (in_stock, current_price, shipping_cost, available_quantity)
            - in_stock=False 表示无库存（包括产品已下架/不可购买）
            - in_stock=None 仅在 API 请求异常时返回
        """
        client = self._get_dajian_client()
        
        try:
            # 1. 获取价格信息（包含 skuAvailable 字段）
            price_info = self._price_cache.get(sku)
            price_api_error = None
            if price_info is None:
                try:
                    price_info = client.get_product_price(sku)
                    if price_info:
                        self._price_cache[sku] = price_info
                except Exception as e:
                    price_api_error = e
                    self.logger.warning(f"  {sku}: 价格 API 异常 ({str(e)[:60]}), 尝试其他 API...")
            
            # 检查产品是否可购买 (skuAvailable=False 表示产品已下架/不可购买)
            if price_info and price_info.get('skuAvailable') is False:
                self.logger.info(f"  {sku}: 大建标记为不可购买 (skuAvailable=False)")
                return False, None, None, 0
            
            # 2. 获取库存信息 (独立于价格 API)
            inventory = self._inventory_cache.get(sku)
            if inventory is None:
                try:
                    inventory = client.get_inventory_by_sku(sku)
                    if inventory:
                        self._inventory_cache[sku] = inventory
                except Exception as e:
                    self.logger.warning(f"  {sku}: 库存 API 异常: {e}")
            
            if not inventory and not price_info:
                # 价格和库存 API 都失败 → 尝试详情 API 作为最终回退
                try:
                    detail = client.get_product_detail_by_sku(sku)
                    if detail and detail.get('skuAvailable') is False:
                        self.logger.info(f"  {sku}: 详情 API 确认不可购买")
                        return False, None, None, 0
                    elif detail and detail.get('skuAvailable') is True:
                        # 详情 API 显示可购买但价格和库存 API 都失败
                        self.logger.warning(f"  {sku}: 详情 API 可购买但价格/库存不可用")
                        return True, None, None, None
                except Exception:
                    pass
                # 全部 API 失败
                if price_api_error:
                    raise price_api_error
                self.logger.warning(f"  {sku}: 所有 API 均返回空数据，视为无库存")
                return False, None, None, 0
            
            if not inventory:
                # 库存 API 失败但价格 API 成功 → 仅基于 skuAvailable 判断
                if price_info and price_info.get('skuAvailable') is True:
                    raw_price = (
                        price_info.get('exclusivePrice')
                        or price_info.get('discountedPrice')
                        or price_info.get('price')
                    )
                    current_price = float(raw_price) if raw_price else None
                    shipping_cost = float(price_info.get('shippingFee') or 0)
                    return True, current_price, shipping_cost, None
                return False, None, None, 0

            buyer_inv = inventory.get('buyerInventoryInfo') or {}
            seller_inv = inventory.get('sellerInventoryInfo') or {}

            total_qty = extract_available_inventory_quantity(inventory)
            in_stock = total_qty > 0
            
            if not price_info:
                # 价格 API 失败但库存 API 成功 → 返回库存状态 + None 价格
                return in_stock, None, None, total_qty
            
            # 解析价格 - 优先使用专享价/折扣价，否则用原价
            raw_price = (
                price_info.get('exclusivePrice')
                or price_info.get('discountedPrice')
                or price_info.get('price')
            )
            if raw_price is None:
                # 所有价格字段都为 null
                # 幽灵产品检测: skuAvailable=True 但无价格也无库存信息
                # 典型场景: 产品在 Dajian 存在但该卖家无权限/无库存分配
                if not buyer_inv and not seller_inv:
                    self.logger.warning(
                        f"  {sku}: 幽灵产品 — skuAvailable=True 但无价格且无库存信息，视为无库存")
                    return False, None, None, 0
                # 有库存信息但无价格 → 返回库存状态 + None 价格
                self.logger.warning(f"  {sku}: 价格数据不可用 (所有价格字段为 null)")
                return in_stock, None, None, total_qty
            
            current_price = float(raw_price)
            shipping_cost = float(price_info.get('shippingFee') or 0)
            
            return in_stock, current_price, shipping_cost, total_qty
            
        except Exception as e:
            self.logger.warning(f"Failed to check Dajian stock for {sku}: {e}")
            return None, None, None, None
    
    def update_ebay_quantity(self, sku: str, quantity: int) -> bool:
        """更新 eBay 库存数量，并回读验证 live offer 状态。"""
        import requests
        from src.services.ebay_auth import EbayOAuthService
        
        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        token = oauth.get_valid_token()
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US'
        }
        
        def _select_offer(offer_list):
            if not offer_list:
                return None
            return sorted(
                offer_list,
                key=lambda o: (
                    0 if (o.get('listing') or {}).get('listingStatus') in ('ACTIVE', 'OUT_OF_STOCK') else 1,
                    0 if o.get('status') == 'PUBLISHED' else 1,
                    0 if o.get('marketplaceId') == 'EBAY_US' else 1,
                    o.get('offerId', ''),
                )
            )[0]

        def _verify_offer_quantity(expected_quantity: int) -> bool:
            def _fetch_inventory_item_quantity() -> Optional[int]:
                try:
                    inventory_resp = requests.get(
                        f"{base}/sell/inventory/v1/inventory_item/{sku}",
                        headers=headers,
                        timeout=30,
                        verify=False,
                    )
                    if inventory_resp.status_code != 200:
                        return None
                    raw_quantity = (
                        ((inventory_resp.json().get('availability') or {})
                         .get('shipToLocationAvailability') or {})
                        .get('quantity')
                    )
                    return int(float(raw_quantity)) if raw_quantity is not None else None
                except Exception:
                    return None

            for attempt in range(1, 5):
                try:
                    verify_resp = requests.get(offer_url, headers=headers, timeout=30, verify=False)
                    if verify_resp.status_code == 200:
                        live_offer = _select_offer(verify_resp.json().get('offers', []))
                        if live_offer:
                            live_available = live_offer.get('availableQuantity')
                            try:
                                live_available = int(float(live_available))
                            except Exception:
                                live_available = None
                            if live_available is None:
                                live_available = _fetch_inventory_item_quantity()
                            listing_status = (live_offer.get('listing') or {}).get('listingStatus')
                            if expected_quantity > 0:
                                if live_available == expected_quantity and listing_status == 'ACTIVE':
                                    return True
                            elif live_available == 0 or listing_status == 'OUT_OF_STOCK':
                                return True
                except Exception:
                    pass
                if attempt < 4:
                    time.sleep(3)
            return False

        def _sanitize_inventory_item_for_quantity_update(inventory_item: dict) -> dict:
            """Remove stale invalid package data that blocks quantity-only updates."""
            pkg = inventory_item.get('packageWeightAndSize')
            if not isinstance(pkg, dict):
                return inventory_item

            weight = pkg.get('weight')
            if isinstance(weight, dict):
                try:
                    value = float(weight.get('value', 0))
                except (TypeError, ValueError):
                    value = 0

                if value <= 0 or value > 2000:
                    pkg.pop('weight', None)
                    self.logger.warning(
                        f"  {sku}: removed invalid package weight before quantity update"
                    )
                else:
                    pkg['weight'] = {
                        'value': round(value, 2),
                        'unit': weight.get('unit') or 'POUND',
                    }

            return inventory_item

        def _verify_trading_available_quantity(expected_quantity: int) -> bool:
            if not listing_id:
                return False
            try:
                import xml.etree.ElementTree as ET
                from src.clients.ebay_client import EbayClient
                from src.clients.ebay_trading_client import EbayTradingClient

                ebay = EbayClient(
                    os.getenv("EBAY_APP_ID"),
                    os.getenv("EBAY_CERT_ID"),
                    os.getenv("EBAY_DEV_ID"),
                    env="production"
                )
                trading = EbayTradingClient(ebay)
                response = trading.call(
                    "GetItem",
                    f"""
                    <ItemID>{listing_id}</ItemID>
                    <DetailLevel>ReturnAll</DetailLevel>
                    """
                )
                root = ET.fromstring(response)
                ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
                item = root.find('.//ebay:Item', ns)
                if item is None:
                    return False

                status = item.findtext(
                    'ebay:SellingStatus/ebay:ListingStatus',
                    default='',
                    namespaces=ns,
                )
                available_text = item.findtext('ebay:QuantityAvailable', default='', namespaces=ns)
                quantity_text = item.findtext('ebay:Quantity', default='', namespaces=ns)
                sold_text = item.findtext(
                    'ebay:SellingStatus/ebay:QuantitySold',
                    default='0',
                    namespaces=ns,
                )
                if available_text not in (None, ''):
                    available = int(float(available_text))
                else:
                    available = max(0, int(float(quantity_text or 0)) - int(float(sold_text or 0)))
                return available == expected_quantity and status == 'Active'
            except Exception as e:
                self.logger.warning(f"Trading quantity verification failed for {sku}: {e}")
                return False

        # 1. 先获取 offer/listing ID
        base = "https://api.ebay.com"
        offer_url = f"{base}/sell/inventory/v1/offer?sku={sku}"
        resp = requests.get(offer_url, headers=headers, timeout=30, verify=False)
        
        if resp.status_code != 200:
            self.logger.error(f"Failed to get offer for {sku}: {resp.status_code}")
            return False
        
        offers = resp.json().get('offers', [])
        if not offers:
            self.logger.error(f"No offer found for {sku}")
            return False

        offer = _select_offer(offers)
        offer_id = offer.get('offerId') if offer else None
        listing_id = (offer or {}).get('listing', {}).get('listingId')
        listing_status = (offer or {}).get('listing', {}).get('listingStatus')
        if not listing_id:
            self.logger.error(f"No listing ID for {sku}")
            return False

        # 2. 更新 Sell Inventory 的库存源
        inventory_ok = False
        try:
            inventory_url = f"{base}/sell/inventory/v1/inventory_item/{sku}"
            inv_resp = requests.get(inventory_url, headers=headers, timeout=30, verify=False)
            if inv_resp.status_code == 200:
                inventory_item = inv_resp.json()
                inventory_item.setdefault('availability', {}).setdefault(
                    'shipToLocationAvailability', {}
                )['quantity'] = quantity
                inventory_item = _sanitize_inventory_item_for_quantity_update(inventory_item)
                put_resp = requests.put(
                    inventory_url,
                    headers=headers,
                    json=inventory_item,
                    timeout=60,
                    verify=False,
                )
                inventory_ok = put_resp.status_code in (200, 204)
                if not inventory_ok:
                    self.logger.warning(
                        f"Inventory item quantity update failed for {sku}: "
                        f"{put_resp.status_code} {put_resp.text[:200]}"
                    )
            else:
                self.logger.warning(f"Failed to get inventory item for {sku}: {inv_resp.status_code}")
        except Exception as e:
            self.logger.warning(f"Inventory item quantity update exception for {sku}: {e}")

        # 3. 更新 offer 的 availableQuantity。仅改 Inventory item 对已售罄刊登不一定会解除 OUT_OF_STOCK。
        offer_ok = False
        if offer_id:
            try:
                offer['availableQuantity'] = quantity
                put_offer = requests.put(
                    f"{base}/sell/inventory/v1/offer/{offer_id}",
                    headers=headers,
                    json=offer,
                    timeout=60,
                    verify=False,
                )
                offer_ok = put_offer.status_code in (200, 204)
                if not offer_ok:
                    self.logger.warning(
                        f"Offer quantity update failed for {sku}: "
                        f"{put_offer.status_code} {put_offer.text[:200]}"
                    )
            except Exception as e:
                self.logger.warning(f"Offer quantity update exception for {sku}: {e}")

        # 4. 同步 Trading 数量，确保传统 listing quantity/sold 视图也一致。
        trading_ok = None
        trading_sync_enabled = str(os.getenv("EBAY_ENABLE_TRADING_QUANTITY_SYNC", "") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        # Sold-through listings can keep Trading Quantity == QuantitySold even after
        # Sell Inventory accepts a positive availableQuantity. In that state eBay
        # leaves the listing OUT_OF_STOCK until ReviseInventoryStatus is called.
        force_trading_restock = quantity > 0 and listing_status == 'OUT_OF_STOCK'
        if trading_sync_enabled or force_trading_restock:
            try:
                from src.clients.ebay_client import EbayClient
                from src.clients.ebay_trading_client import EbayTradingClient

                ebay = EbayClient(
                    os.getenv("EBAY_APP_ID"),
                    os.getenv("EBAY_CERT_ID"),
                    os.getenv("EBAY_DEV_ID"),
                    env="production"
                )
                trading = EbayTradingClient(ebay)

                xml_body = f"""
                <InventoryStatus>
                    <ItemID>{listing_id}</ItemID>
                    <Quantity>{quantity}</Quantity>
                </InventoryStatus>
                """

                response = trading.call("ReviseInventoryStatus", xml_body)

                if '<Ack>Success</Ack>' in response or '<Ack>Warning</Ack>' in response:
                    trading_ok = True
                else:
                    trading_ok = False
                    self.logger.warning(f"Trading API failed for {sku}: {response[:200]}")

            except Exception as e:
                trading_ok = False
                self.logger.warning(f"Failed to update {sku} quantity via Trading API: {e}")

        if _verify_offer_quantity(quantity):
            self.logger.info(
                f"Updated {sku} quantity to {quantity} "
                f"(inventory={inventory_ok}, offer={offer_ok}, trading={trading_ok})"
            )
            return True

        if quantity > 0 and trading_ok and _verify_trading_available_quantity(quantity):
            self.logger.info(
                f"Updated {sku} quantity to {quantity}; Sell offer status is still catching up "
                f"(inventory={inventory_ok}, offer={offer_ok}, trading={trading_ok})"
            )
            return True

        self.logger.error(
            f"Quantity update for {sku} was not verified "
            f"(inventory={inventory_ok}, offer={offer_ok}, trading={trading_ok})"
        )
        return False
    
    def update_ebay_price(self, sku: str, new_price: float) -> bool:
        """更新 eBay 售价 (支持自动处理促销阻止 + 触底自动关广告).

        🛡️ 安全护栏 (2026-05): 写出前调用 PricingEngine.assert_safe_price 校验.
        若 new_price 低于 SKU 总到岸成本对应的"绝对死线" (含 5% 折扣 + 18.25%
        eBay 费 + $0.30 固定费), 直接拒绝并写日志, 不调任何 eBay API.

        🎯 广告自适应 (2026-05): 5% 广告费率不是钉死的. 当 new_price < 带广告死线
        但 >= 关广告死线时:
          - 若 listing 当前在广告中 → 自动关广告, 然后放行改价 (维持竞争力)
          - 若 listing 不在广告中 → 直接放行 (本就没付广告费)
        关广告/价改顺序: 先 delete_ad → 再 PUT offer, 避免转化期同时收到广告费.
        """
        # ── 守门员: 死线 + 广告自适应 (统一逻辑见 src/services/repricing_guard.py) ──
        try:
            from src.services.repricing_guard import precheck_price
            ok, reason = precheck_price(
                sku=sku, new_price=new_price,
                db_path=self.db_path, allow_ad_disable=True,
            )
            if not ok:
                # precheck_price 已写过详细 ERROR 日志, 这里直接拒绝
                return False
        except Exception as guard_exc:
            # 守门员自身异常不能阻塞改价 (fail-open), 但要记录
            self.logger.warning(f"⚠️ [PRICE GUARD] {sku}: 守门员异常, 放行: {guard_exc}")

        import requests
        from src.services.ebay_auth import EbayOAuthService
        
        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        token = oauth.get_valid_token()
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US',
        }
        
        # 获取 offer
        url = f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}"
        resp = requests.get(url, headers=headers, timeout=30, verify=False)
        
        if resp.status_code != 200:
            return False
        
        offers = resp.json().get('offers', [])
        if not offers:
            return False
        
        offer = offers[0]
        offer_id = offer['offerId']
        
        # 更新价格
        offer['pricingSummary']['price']['value'] = str(round(new_price, 2))
        
        url = f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}"
        resp = requests.put(url, headers=headers, json=offer, timeout=30, verify=False)
        
        if resp.status_code in [200, 204]:
            self.logger.info(f"Updated {sku} price to ${new_price:.2f}")
            return True
        else:
            # 检查是否被促销阻止
            err_msg = ''
            try:
                err_data = resp.json()
                err_msg = err_data.get('errors', [{}])[0].get('message', '')
            except Exception:
                pass

            is_promo_block = ('sale' in err_msg.lower()
                             or 'promotion' in err_msg.lower()
                             or 'markdown' in err_msg.lower())

            if is_promo_block:
                self.logger.warning(f"{sku}: 促销阻止改价, 尝试 promotion-aware 更新...")
                try:
                    from src.services.ebay_discount_service import EbayDiscountService
                    disc_svc = EbayDiscountService()
                    pa_result = disc_svc.update_price_through_promotion(sku, new_price)
                    if pa_result.get('price_updated'):
                        promo_status = '已恢复' if pa_result.get('promotion_restored') else '需手动恢复'
                        self.logger.info(
                            f"✅ {sku}: 促销模式改价成功 ${new_price:.2f} (促销{promo_status})")
                        return True
                    else:
                        self.logger.error(
                            f"❌ {sku}: 促销模式改价失败: {pa_result.get('error', '?')}")
                        return False
                except Exception as e:
                    self.logger.error(f"❌ {sku}: promotion-aware 更新异常: {e}")
                    return False
            else:
                self.logger.error(f"Failed to update {sku} price: {resp.status_code} - {err_msg[:150]}")
            return False

    def get_live_offer_price(self, sku: str) -> Optional[float]:
        """读取当前 eBay offer 的实时售价。"""
        import requests
        from src.services.ebay_auth import EbayOAuthService

        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        token = oauth.get_valid_token()

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

        url = f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}"
        resp = requests.get(url, headers=headers, timeout=30, verify=False)
        if resp.status_code != 200:
            return None

        offers = resp.json().get('offers', [])
        if not offers:
            return None

        price_value = offers[0].get('pricingSummary', {}).get('price', {}).get('value')
        return float(price_value) if price_value is not None else None

    def verify_ebay_price(self, sku: str, expected_price: float, tolerance: float = 0.05) -> Tuple[bool, Optional[float]]:
        """确认 eBay live offer 价格已经反映最新改价。"""
        live_price = None
        for attempt in range(1, 3):
            live_price = self.get_live_offer_price(sku)
            if live_price is not None and abs(live_price - expected_price) <= tolerance:
                return True, live_price
            if attempt < 2:
                time.sleep(1.0)
        return False, live_price
    
    def check_ebay_listing_status(self, sku: str) -> str:
        """
        检查 eBay 商品状态
        
        Returns:
            'ACTIVE', 'ENDED', 'OUT_OF_STOCK', 'NOT_FOUND'
        """
        import requests
        from src.services.ebay_auth import EbayOAuthService
        
        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        token = oauth.get_valid_token()
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }
        
        # 检查 offer 状态
        url = f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}"
        resp = requests.get(url, headers=headers, timeout=30, verify=False)
        
        if resp.status_code != 200:
            return 'NOT_FOUND'
        
        offers = resp.json().get('offers', [])
        if not offers:
            return 'NOT_FOUND'
        
        offer = offers[0]
        status = offer.get('status', 'UNKNOWN')
        
        return status
    
    def calculate_new_price(self, product_price: float, shipping_cost: float, 
                           target_margin: float = 0.15) -> float:
        """
        根据大建成本计算新售价
        
        Args:
            product_price: 大建产品价格
            shipping_cost: 大建运费
            target_margin: 目标利润率 (默认 15%)
        
        Returns:
            建议售价
        """
        from src.services.pricing_engine import PricingEngine
        
        cost_breakdown = PricingEngine.calculate_dajian_cost(product_price, shipping_cost)
        total_cost = cost_breakdown['total_dajian_cost']
        
        selling_price = PricingEngine.calculate_selling_price(
            total_cost, 
            target_margin=target_margin
        )
        
        return selling_price['selling_price']
    
    def _check_and_update_price(
        self,
        sku: str,
        product: Dict,
        current_price: Optional[float],
        shipping_cost: Optional[float],
        dry_run: bool,
    ) -> Optional[float]:
        """
        检查大建价格是否变化，如有变化则更新 eBay 售价和数据库。

        Returns:
            新售价（float）如果更新了，否则 None。
        """
        # 价格数据不可用 → 跳过价格对比（安全保护：不能用 0 去计算售价）
        if current_price is None or current_price <= 0:
            return None

        old_cost = product.get('cost_breakdown', {})
        old_base_cost = old_cost.get('base_cost', 0) or 0

        new_base_cost = current_price + (shipping_cost or 0)

        # 必须有旧成本作为对比基准，且差异超过阈值
        if old_base_cost <= 0 or abs(new_base_cost - old_base_cost) <= 0.01:
            return None

        new_selling_price = self.calculate_new_price(current_price, shipping_cost or 0)

        if not dry_run:
            ebay_ok = self.update_ebay_price(sku, new_selling_price)
            if ebay_ok:
                verified, live_price = self.verify_ebay_price(sku, new_selling_price)
                if verified:
                    self.update_product_cost_in_db(sku, current_price, shipping_cost or 0, new_selling_price)
                else:
                    self.logger.warning(
                        f"  {sku}: eBay 改价返回成功但未通过 live 校验 "
                        f"(live={live_price}, expected={new_selling_price:.2f})"
                    )
                    raise RuntimeError(f"eBay price verification failed for {sku}")
            else:
                self.logger.warning(f"  {sku}: eBay 改价失败，跳过 DB 更新以保持数据一致")
                raise RuntimeError(f"eBay price update failed for {sku}")

        return new_selling_price
    
    def _load_favorites_set(self) -> set:
        """加载大建收藏夹全量SKU集合，用于交叉验证产品是否存在"""
        try:
            client = self._get_dajian_client()
            favorites = client.get_favorites(page_size=1000, max_pages=50)
            fav_skus = {r['sku'] for r in favorites}
            self.logger.info(f"收藏夹已加载: {len(fav_skus)} 个 SKU")
            return fav_skus
        except ValueError:
            self.logger.warning("无法加载收藏夹: 缺少 DAJIAN API 凭证")
            return set()
        except Exception as e:
            self.logger.warning(f"加载收藏夹失败: {e}")
            return set()
    
    def sync_all(self, dry_run: bool = False, skip_ebay_check: bool = True, 
                 limit: int = 0, skip_synced_today: bool = True) -> List[SyncResult]:
        """
        同步所有已发布产品
        
        Args:
            dry_run: 是否为测试模式（不实际更新）
            skip_ebay_check: 跳过eBay状态检查（加速同步）
            limit: 限制同步数量，0=全部
            skip_synced_today: 跳过今日已同步的SKU
        
        Returns:
            同步结果列表
        """
        results = []
        products = self.get_published_products()
        self.last_sync_scope_count = len(products)
        self.last_sync_skipped_count = 0
        
        # 过滤今日已同步的 SKU
        skipped_count = 0
        if skip_synced_today:
            already_synced = self.get_skus_synced_today()
            if already_synced:
                original_count = len(products)
                products = [p for p in products if p['sku'] not in already_synced]
                skipped_count = original_count - len(products)
                self.last_sync_skipped_count = skipped_count
                self.logger.info(f"跳过今日已同步的 {skipped_count} 个产品")
        
        if limit > 0:
            products = products[:limit]
        
        self.logger.info(f"开始同步 {len(products)} 个已发布产品 (skip_ebay_check={skip_ebay_check}, 跳过今日已同步={skipped_count})...")
        
        # 预加载收藏夹SKU集合 — 用于交叉验证产品是否仍存在于平台
        favorites_set = self._load_favorites_set()
        try:
            self._prefetch_dajian_data([p['sku'] for p in products])
        except Exception as e:
            self.logger.warning(f"预加载大建缓存失败，将回退到单 SKU 查询: {e}")
        
        for i, product in enumerate(products):
            sku = product['sku']
            try:
                result = self._sync_single_product(product, dry_run, skip_ebay_check, favorites_set)
                results.append(result)
                
                # 记录同步结果到数据库
                if not dry_run and result.action in ('out_of_stock', 'price_updated', 'no_change', 'restocked', 'skipped', 'data_missing'):
                    self.record_sync(result)
                
                # 每10个产品记录一次进度
                if (i + 1) % 10 == 0:
                    self.logger.info(f"  进度: {i+1}/{len(products)}")
            except Exception as e:
                self.logger.error(f"同步 {sku} 出错: {e}")
                results.append(SyncResult(sku=sku, action='error', message=str(e)))
        
        return results
    
    def _sync_single_product(self, product: Dict, dry_run: bool, 
                              skip_ebay_check: bool = True,
                              favorites_set: set = None) -> SyncResult:
        """同步单个产品"""
        sku = product['sku']

        def _set_ebay_quantity_zero() -> bool:
            """Return whether the requested zero-quantity write succeeded."""
            return dry_run or bool(self.update_ebay_quantity(sku, 0))
        
        try:
            # 1. 可选：检查 eBay 状态（跳过可加速同步）
            if not skip_ebay_check:
                ebay_status = self.check_ebay_listing_status(sku)
                
                if ebay_status == 'ENDED':
                    return SyncResult(
                        sku=sku,
                        action='skipped',
                        message='eBay 链接已下架'
                    )
                
                if ebay_status == 'NOT_FOUND':
                    return SyncResult(
                        sku=sku,
                        action='skipped',
                        message='eBay 未找到 offer'
                    )
            
            # 2. 检查大建库存和价格
            in_stock, current_price, shipping_cost, available_quantity = self.check_dajian_stock(sku)
            
            if in_stock is None:
                # API 请求异常 — 用收藏夹交叉验证产品是否存在
                if favorites_set and sku not in favorites_set:
                    # 产品不在收藏夹 → 已下架/停产，确认无库存
                    self.logger.warning(f"  {sku}: API异常且不在收藏夹 → 产品已停产，下架")
                    if not _set_ebay_quantity_zero():
                        return SyncResult(
                            sku=sku,
                            action='error',
                            message='产品已从大建平台下架，但 eBay 库存归零失败',
                            supplier_in_stock=None,
                        )
                    return SyncResult(
                        sku=sku,
                        action='out_of_stock',
                        old_value='有库存',
                        new_value='库存设为0',
                        message='产品已从大建平台下架 (不在收藏夹中)',
                        supplier_in_stock=None,
                    )
                
                # 产品在收藏夹中但API异常 → 短暂故障，用连续失败计数
                consecutive_skips = self._get_consecutive_skip_count(sku)
                if consecutive_skips >= 2:
                    self.logger.warning(
                        f"  {sku}: 连续 {consecutive_skips+1} 次无法获取库存信息，视为无库存并下架")
                    if not _set_ebay_quantity_zero():
                        return SyncResult(
                            sku=sku,
                            action='error',
                            message='连续获取大建库存失败，且 eBay 库存归零失败',
                            supplier_in_stock=None,
                        )
                    return SyncResult(
                        sku=sku,
                        action='out_of_stock',
                        old_value='有库存',
                        new_value='库存设为0',
                        message=f'连续 {consecutive_skips+1} 次无法获取大建库存，已将 eBay 库存设为 0',
                        supplier_in_stock=None,
                    )
                return SyncResult(
                    sku=sku,
                    action='skipped',
                    message=f'无法获取大建库存信息 (连续第 {consecutive_skips+1} 次)'
                )
            
            # 3. 处理无库存情况
            if not in_stock:
                if not _set_ebay_quantity_zero():
                    return SyncResult(
                        sku=sku,
                        action='error',
                        message='大建无库存，但 eBay 库存归零失败',
                        supplier_in_stock=False,
                    )
                
                return SyncResult(
                    sku=sku,
                    action='out_of_stock',
                    old_value='有库存',
                    new_value='库存设为0',
                    message='大建无库存，已将 eBay 库存设为 0',
                    supplier_in_stock=False,
                )
            
            # 3.5 检查是否从缺货恢复 → 自动重新上架
            last_action = self.get_last_sync_action(sku)
            if last_action == 'out_of_stock' and in_stock:
                self.logger.info(f"  {sku}: 从缺货恢复，重新上架")
                restore_quantity = 1
                restored = True
                if not dry_run:
                    restored = bool(self.update_ebay_quantity(sku, restore_quantity))
                if not restored:
                    return SyncResult(
                        sku=sku,
                        action='error',
                        message='大建已补货，但 eBay 库存恢复失败',
                        supplier_in_stock=True,
                    )
                
                # 同时检查价格是否有变化
                price_msg = ""
                price_result = self._check_and_update_price(
                    sku, product, current_price, shipping_cost, dry_run
                )
                if price_result:
                    price_msg = f"，价格已更新为${price_result:.2f}"
                
                return SyncResult(
                    sku=sku,
                    action='restocked',
                    old_value='库存为0',
                    new_value=f'库存恢复为{restore_quantity}',
                    message=f'大建已补货，eBay 库存已恢复为 {restore_quantity}{price_msg}',
                    supplier_in_stock=True,
                )
            
            # 4. 检查是否只有库存但无价格数据 (幽灵/异常状态)
            if current_price is None and in_stock:
                self.logger.warning(
                    f"  {sku}: 有库存但价格数据不可用，标记为 data_missing")
                return SyncResult(
                    sku=sku,
                    action='data_missing',
                    message='大建有库存但价格数据不可用，请人工检查',
                    supplier_in_stock=True,
                )

            # 5. 检查价格变化
            price_result = self._check_and_update_price(
                sku, product, current_price, shipping_cost, dry_run
            )
            if price_result:
                old_cost = product.get('cost_breakdown', {})
                old_base_cost = old_cost.get('base_cost', 0) or 0
                new_base_cost = (current_price or 0) + (shipping_cost or 0)
                return SyncResult(
                    sku=sku,
                    action='price_updated',
                    old_value=f'成本${old_base_cost:.2f} (商品${old_cost.get("product_price", 0):.2f}+运费${old_cost.get("shipping_cost", 0):.2f})',
                    new_value=f'成本${new_base_cost:.2f} (商品${current_price:.2f}+运费${shipping_cost or 0:.2f}), 新售价${price_result:.2f}',
                    message='大建价格变化，已更新 eBay 售价',
                    supplier_in_stock=True,
                )
            
            # 6. 无变化
            return SyncResult(
                sku=sku,
                action='no_change',
                message='库存正常，价格无变化',
                supplier_in_stock=True,
            )
            
        except Exception as e:
            return SyncResult(
                sku=sku,
                action='error',
                message=str(e),
                supplier_in_stock=None,
            )
