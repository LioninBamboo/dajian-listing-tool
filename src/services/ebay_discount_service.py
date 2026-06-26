"""
eBay 店铺折扣/促销管理服务

功能：
1. 创建 Item Price Markdown 促销 (全店或指定链接)
2. 获取活跃促销列表
3. 删除/暂停促销
4. 本地折扣状态缓存 — 同步到 Dashboard 价格和利润率计算

eBay API:
- POST   /sell/marketing/v1/item_price_markdown  — 创建 markdown 促销
- GET    /sell/marketing/v1/item_price_markdown   — 获取促销列表
- DELETE /sell/marketing/v1/item_price_markdown/{promotion_id} — 删除促销
- GET    /sell/marketing/v1/promotion         — 获取所有促销
"""
import os
import sys
import json
import time
import logging
import requests
import warnings
from pathlib import Path
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)

# 折扣缓存文件
DISCOUNT_CACHE_FILE = PROJECT_ROOT / 'logs' / '_discount_cache.json'
DISCOUNT_CACHE_TTL_HOURS = 4


class EbayDiscountService:
    """eBay 店铺折扣/促销管理"""

    def __init__(self):
        from src.services.ebay_auth import EbayOAuthService
        self.oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
        self.base = self.oauth.api_base

    def _headers(self):
        token = self.oauth.get_valid_token()
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        }

    def _inventory_headers(self):
        headers = self._headers()
        headers['Content-Language'] = 'en-US'
        return headers

    def _api_request(self, method, url, json_body=None, retries=3):
        """通用 API 请求，带重试"""
        for attempt in range(retries):
            try:
                r = requests.request(
                    method, url, headers=self._headers(), json=json_body,
                    timeout=60, verify=False
                )
                return r
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(3)
                    continue
                logger.error(f"API {method} {url} 失败: {e}")
                return None

    # ─── 获取促销列表 ───

    def fetch_promotions(self, status='RUNNING'):
        """
        获取所有促销活动

        Args:
            status: RUNNING, PAUSED, ENDED, etc. (逗号分隔可多值)
        Returns:
            list of promotion dicts
        """
        promotions = []
        offset = 0
        # 将请求的状态列表转为 set 用于客户端二次过滤
        # eBay API 有时不严格按 status 过滤, 需要客户端再筛一遍
        requested_statuses = set(s.strip() for s in status.split(','))
        while True:
            r = None
            for attempt in range(3):
                try:
                    r = requests.get(
                        f"{self.base}/sell/marketing/v1/promotion",
                        headers=self._headers(),
                        params={
                            'marketplace_id': 'EBAY_US',
                            'status': status,
                            'limit': '200',
                            'offset': str(offset),
                        },
                        timeout=60, verify=False
                    )
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"获取促销列表异常, 重试中 ({attempt+1}/3): {e}")
                        time.sleep(3)
                        continue
                    logger.error(f"获取促销列表失败 (已重试3次): {e}")
                    return promotions
            if r.status_code != 200:
                logger.warning(f"获取促销列表失败: {r.status_code} {r.text[:300]}")
                break
            data = r.json()
            batch = data.get('promotions', [])
            # 客户端过滤: 仅保留请求状态匹配的促销
            for p in batch:
                if p.get('promotionStatus') in requested_statuses:
                    promotions.append(p)
            total = data.get('total', 0)
            if offset + len(batch) >= total or not batch:
                break
            offset += len(batch)
        return promotions

    def fetch_markdown_promotions(self):
        """获取所有折扣促销 (Markdown Sale + Order Discount)"""
        all_promos = self.fetch_promotions('RUNNING,SCHEDULED,PAUSED')
        discounts = [
            p for p in all_promos
            if p.get('promotionType') in ('MARKDOWN_SALE', 'ITEM_PRICE_MARKDOWN', 'ORDER_DISCOUNT')
            or 'markdown' in (p.get('name', '') or '').lower()
            or 'discount' in (p.get('name', '') or '').lower()
        ]
        return discounts

    # ─── 创建 Markdown 促销 ───

    def _get_all_active_listing_ids(self):
        """获取所有已刊登产品的 listing_id"""
        import sqlite3
        db_path = PROJECT_ROOT / 'ebay_collection.db'
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT listing_id FROM collected_products WHERE status = 'PUBLISHED' AND listing_id IS NOT NULL"
        ).fetchall()
        conn.close()
        return [str(r[0]) for r in rows if r[0]]

    def create_markdown_sale(self, name, discount_pct, listing_ids=None,
                              start_date=None, end_date=None, auto_select_all=True):
        """
        创建 Item Price Markdown 促销

        eBay API: POST /sell/marketing/v1/item_price_markdown
        Body 结构:
          selectedInventoryDiscounts: [{
              discountBenefit: { percentageOffItem: "5.0" },
              ruleOrder: 0,
              inventoryCriterion: {
                  inventoryCriterionType: "INVENTORY_BY_VALUE",
                  listingIds: [...]
              }
          }]
        注意: eBay 不支持 INVENTORY_ANY, 必须用 INVENTORY_BY_VALUE + 具体 listing IDs

        Args:
            name: 促销名称
            discount_pct: 折扣百分比 (e.g., 5.0 = 5% off)
            listing_ids: 指定链接列表 (None = 全店)
            start_date: 开始时间 (默认现在+5分钟)
            end_date: 结束时间 (默认2天后)
            auto_select_all: 如果 listing_ids 为 None，是否自动选全部

        Returns:
            dict with success/error info
        """
        if not start_date:
            start_date = datetime.now(timezone.utc) + timedelta(minutes=5)
        if not end_date:
            end_date = start_date + timedelta(days=2)

        # eBay 要求 ISO 8601 UTC 格式
        start_str = start_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        end_str = end_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        # 全店促销: 获取所有 active listing IDs
        if not listing_ids and auto_select_all:
            listing_ids = self._get_all_active_listing_ids()
            if not listing_ids:
                return {'success': False, 'error': '没有找到已刊登的产品 listing ID'}
            logger.info(f"全店促销: 共 {len(listing_ids)} 个 listing")

        # 构建 selectedInventoryDiscounts — eBay item_price_markdown 必需字段
        # 注意: 使用 ONE entry + INVENTORY_BY_VALUE + 所有 listingIds
        # 格式与 eBay Seller Hub 创建的促销完全一致
        selected_discounts = [{
            "discountBenefit": {
                "percentageOffItem": str(discount_pct),
            },
            "ruleOrder": 0,
            "inventoryCriterion": {
                "inventoryCriterionType": "INVENTORY_BY_VALUE",
                "listingIds": [str(lid) for lid in listing_ids] if listing_ids else [],
            },
        }]

        body = {
            "name": name,
            "description": f"Store-wide {discount_pct}% discount",
            "startDate": start_str,
            "endDate": end_str,
            "marketplaceId": "EBAY_US",
            "promotionStatus": "SCHEDULED",
            "promotionImageUrl": "https://ir.ebaystatic.com/pictures/hk/shipping/Blank_Pic_Test.png",
            "blockPriceIncreaseInItemRevision": False,
            "applyFreeShipping": False,
            "autoSelectFutureInventory": auto_select_all,
            "selectedInventoryDiscounts": selected_discounts,
        }

        url = f"{self.base}/sell/marketing/v1/item_price_markdown"

        try:
            r = requests.post(url, headers=self._headers(), json=body,
                             timeout=120, verify=False)

            if r.status_code in (200, 201):
                promo_id = ''
                # promotion ID may be in Location header or response body
                if 'Location' in r.headers:
                    promo_id = r.headers['Location'].split('/')[-1]
                elif r.text:
                    try:
                        resp = r.json()
                        promo_id = resp.get('promotionId', '')
                    except:
                        pass
                logger.info(f"✅ 创建 Markdown 促销成功: {name}, {discount_pct}% off, "
                           f"ID={promo_id}")
                result = {
                    'success': True,
                    'promotion_id': promo_id,
                    'name': name,
                    'discount_pct': discount_pct,
                    'start_date': start_str,
                    'end_date': end_str,
                    'listing_count': len(listing_ids) if listing_ids else 'ALL',
                }
                # 保存到本地缓存
                self._save_discount_state(result)
                return result
            else:
                err = r.text[:500]
                logger.error(f"❌ 创建 Markdown 促销失败 {r.status_code}: {err}")

                # errorId 2003 (ACCESS/APPLICATION) = eBay API 不支持通过 API 创建促销
                # 回退方案: 保存到本地，建议用户通过 Seller Hub 创建
                is_access_error = '2003' in err or ('ACCESS' in err and 'Internal error' in err)
                if is_access_error:
                    logger.warning("⚠️ eBay Marketing API 写入不可用 (errorId 2003)，"
                                   "回退到本地模式")
                    # 尝试同步 eBay 已有的 RUNNING 促销
                    synced = self.sync_running_promotions(override_discount=discount_pct)
                    if synced:
                        return {
                            'success': True,
                            'promotion_id': synced.get('promotion_id', 'SYNCED'),
                            'name': synced.get('name', name),
                            'discount_pct': discount_pct,
                            'start_date': synced.get('start_date', start_str),
                            'end_date': synced.get('end_date', end_str),
                            'listing_count': synced.get('listing_count', 'ALL'),
                            'mode': 'synced_from_ebay',
                        }
                    # 没有已有促销，保存到本地
                    local_result = {
                        'success': True,
                        'promotion_id': 'LOCAL',
                        'name': name,
                        'discount_pct': discount_pct,
                        'start_date': start_str,
                        'end_date': end_str,
                        'listing_count': len(listing_ids) if listing_ids else 'ALL',
                        'mode': 'local_only',
                    }
                    self._save_discount_state(local_result)
                    return local_result

                return {'success': False, 'error': f"HTTP {r.status_code}: {err}"}
        except Exception as e:
            logger.error(f"❌ 创建促销异常: {e}")
            return {'success': False, 'error': str(e)}

    def sync_running_promotions(self, override_discount=None):
        """
        从 eBay 同步当前运行中的促销到本地缓存
        支持: ORDER_DISCOUNT, MARKDOWN_SALE, ITEM_PRICE_MARKDOWN
        优先: 覆盖全店的 ORDER_DISCOUNT > 单品 MARKDOWN_SALE

        Args:
            override_discount: 如果 eBay 不返回折扣百分比，使用此值

        Returns:
            dict: 同步的促销信息, 或 None
        """
        SUPPORTED_TYPES = ('ORDER_DISCOUNT', 'MARKDOWN_SALE', 'ITEM_PRICE_MARKDOWN')
        try:
            promos = self.fetch_promotions('RUNNING')
            candidates = [p for p in promos if p.get('promotionType') in SUPPORTED_TYPES]
            if not candidates:
                logger.info("eBay 上没有运行中的折扣促销")
                return None

            # 优先选全店 ORDER_DISCOUNT，然后 MARKDOWN_SALE
            best = None
            for p in candidates:
                if p.get('promotionType') == 'ORDER_DISCOUNT':
                    best = p
                    break  # ORDER_DISCOUNT 覆盖全店，优先级最高
            if not best:
                best = candidates[0]

            promo_type = best.get('promotionType', '')
            promo_id = best.get('promotionId', '')
            logger.info(f"选中促销: {best.get('name')} (type={promo_type}, id={promo_id})")

            # 获取促销详情 (列表接口不返回折扣信息，需要单独 GET)
            detail = self._fetch_promotion_detail(promo_id, promo_type)
            if detail:
                disc_pct = self._extract_discount_pct(detail)
            else:
                disc_pct = self._extract_discount_pct(best)

            if disc_pct <= 0 and override_discount:
                disc_pct = float(override_discount)

            promo_data = {
                'promotion_id': promo_id,
                'name': best.get('name', 'eBay Promotion'),
                'discount_pct': disc_pct,
                'start_date': best.get('startDate', ''),
                'end_date': best.get('endDate', ''),
                'listing_count': 'ALL',
                'promotion_type': promo_type,
            }
            self._save_discount_state(promo_data)
            logger.info(f"✅ 已同步 eBay 运行中促销: {promo_data['name']} "
                        f"({promo_data['discount_pct']}% off, type={promo_type})")
            return promo_data
        except Exception as e:
            logger.warning(f"同步促销失败: {e}")
            return None

    def _fetch_promotion_detail(self, promo_id, promo_type):
        """
        获取促销详情
        ORDER_DISCOUNT → /item_promotion/{id}
        MARKDOWN_SALE  → /item_price_markdown/{id}
        """
        if promo_type == 'ORDER_DISCOUNT':
            url = f"{self.base}/sell/marketing/v1/item_promotion/{promo_id}"
        else:
            url = f"{self.base}/sell/marketing/v1/item_price_markdown/{promo_id}"
        try:
            r = requests.get(url, headers=self._headers(), timeout=60, verify=False)
            if r.status_code == 200:
                return r.json()
            logger.warning(f"获取促销详情失败: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.warning(f"获取促销详情异常: {e}")
        return None

    def _extract_discount_pct(self, promo):
        """从促销对象提取折扣百分比 (支持多种促销类型)"""
        # 1. discountRules → percentageOffOrder (ORDER_DISCOUNT) 或 percentageOffItem
        rules = promo.get('discountRules', [])
        for rule in rules:
            benefit = rule.get('discountBenefit', {})
            # ORDER_DISCOUNT 用 percentageOffOrder
            pct = benefit.get('percentageOffOrder', 0)
            if pct:
                return float(pct)
            # MARKDOWN_SALE 用 percentageOffItem
            pct = benefit.get('percentageOffItem', 0)
            if pct:
                return float(pct)
        # 2. selectedInventoryDiscounts (MARKDOWN_SALE 详情)
        sids = promo.get('selectedInventoryDiscounts', [])
        for sid in sids:
            benefit = sid.get('discountBenefit', {})
            pct = benefit.get('percentageOffItem', 0)
            if pct:
                return float(pct)
            disc = sid.get('discount', {})
            pct = disc.get('percentage', 0)
            if pct:
                return float(pct)
        return 0.0

    # ─── 删除/暂停促销 ───

    def delete_promotion(self, promotion_id):
        """删除促销"""
        url = f"{self.base}/sell/marketing/v1/item_price_markdown/{promotion_id}"
        try:
            r = requests.delete(url, headers=self._headers(), timeout=30, verify=False)
            if r.status_code in (200, 204):
                logger.info(f"✅ 已删除促销: {promotion_id}")
                # 清除本地缓存
                self._clear_discount_state()
                return {'success': True, 'promotion_id': promotion_id}
            else:
                err = r.text[:300]
                logger.error(f"❌ 删除促销失败 {r.status_code}: {err}")
                return {'success': False, 'error': f"HTTP {r.status_code}: {err}"}
        except Exception as e:
            logger.error(f"❌ 删除促销异常: {e}")
            return {'success': False, 'error': str(e)}

    def pause_promotion(self, promotion_id):
        """暂停促销"""
        url = f"{self.base}/sell/marketing/v1/item_price_markdown/{promotion_id}/pause"
        try:
            r = requests.post(url, headers=self._headers(), timeout=30, verify=False)
            if r.status_code in (200, 204):
                logger.info(f"✅ 已暂停促销: {promotion_id}")
                return {'success': True, 'promotion_id': promotion_id}
            else:
                err = r.text[:300]
                logger.error(f"❌ 暂停促销失败 {r.status_code}: {err}")
                return {'success': False, 'error': f"HTTP {r.status_code}: {err}"}
        except Exception as e:
            logger.error(f"❌ 暂停促销异常: {e}")
            return {'success': False, 'error': str(e)}

    def resume_promotion(self, promotion_id):
        """恢复促销"""
        url = f"{self.base}/sell/marketing/v1/item_price_markdown/{promotion_id}/resume"
        try:
            r = requests.post(url, headers=self._headers(), timeout=30, verify=False)
            if r.status_code in (200, 204):
                logger.info(f"✅ 已恢复促销: {promotion_id}")
                return {'success': True, 'promotion_id': promotion_id}
            else:
                err = r.text[:300]
                logger.error(f"❌ 恢复促销失败 {r.status_code}: {err}")
                return {'success': False, 'error': f"HTTP {r.status_code}: {err}"}
        except Exception as e:
            logger.error(f"❌ 恢复促销异常: {e}")
            return {'success': False, 'error': str(e)}

    # ─── 本地折扣状态管理 ───

    # ─── Promotion-Aware Price Update ───

    def get_active_promotions_for_listing(self, listing_id: str):
        """
        获取覆盖指定 listing 的活跃 MARKDOWN_SALE 促销

        Returns:
            list of promotion dicts (含 promotion_id, discount_pct, end_date 等)
        """
        # 获取 RUNNING + SCHEDULED 状态 (两者都可能阻止改价)
        promos = self.fetch_promotions('RUNNING,SCHEDULED')
        # MARKDOWN_SALE / ITEM_PRICE_MARKDOWN 会锁定 listing 价格
        # ORDER_DISCOUNT 不会锁定, 无需处理
        blocking = [
            p for p in promos
            if p.get('promotionType') in ('MARKDOWN_SALE', 'ITEM_PRICE_MARKDOWN')
        ]
        return blocking

    def update_price_through_promotion(self, sku: str, new_price: float,
                                        listing_id: str = None) -> dict:
        """
        Promotion-aware 价格更新

        流程:
        1. 尝试正常 PUT offer 改价
        2. 如果遇到 promotion 阻止错误:
           a. 获取活跃 Markdown 促销信息 (promotion_id, discount_pct, end_date)
           b. 删除该促销
           c. 等待 eBay 处理 (3秒)
           d. 再次 PUT offer 改价
           e. 用相同参数重新创建促销

        Args:
            sku: 产品 SKU
            new_price: 新的基础售价
            listing_id: listing ID (可选, 用于匹配促销)

        Returns:
            dict: {
                'success': bool,
                'price_updated': bool,
                'promotion_handled': bool,
                'promotion_restored': bool,
                'error': str (if any),
            }
        """
        result = {
            'success': False,
            'price_updated': False,
            'promotion_handled': False,
            'promotion_restored': False,
            'error': '',
        }

        # Step 1: 尝试正常改价
        try:
            offer_url = f"{self.base}/sell/inventory/v1/offer?sku={sku}&marketplace_id=EBAY_US"
            resp = requests.get(offer_url, headers=self._headers(), timeout=30, verify=False)
            if resp.status_code != 200:
                result['error'] = f'GET offer failed: {resp.status_code}'
                return result

            offers = resp.json().get('offers', [])
            if not offers:
                result['error'] = f'No offer found for {sku}'
                return result

            offer = offers[0]
            offer_id = offer['offerId']
            if not listing_id:
                listing_id = offer.get('listing', {}).get('listingId', '')

            # 更新价格
            offer['pricingSummary']['price']['value'] = str(round(new_price, 2))
            put_url = f"{self.base}/sell/inventory/v1/offer/{offer_id}"
            resp = requests.put(
                put_url,
                headers=self._inventory_headers(),
                json=offer,
                timeout=60,
                verify=False,
            )

            if resp.status_code in (200, 204):
                result['success'] = True
                result['price_updated'] = True
                logger.info(f"[{sku}] 价格已更新: ${new_price:.2f}")
                return result

            # 检查是否是 promotion 阻止
            err_msg = ''
            try:
                err_data = resp.json()
                errors = err_data.get('errors', [])
                err_msg = errors[0].get('message', '') if errors else ''
            except Exception:
                err_msg = resp.text[:300]

            is_promo_block = ('sale' in err_msg.lower()
                             or 'promotion' in err_msg.lower()
                             or 'markdown' in err_msg.lower())

            if not is_promo_block:
                result['error'] = f'PUT offer failed: {resp.status_code} - {err_msg[:150]}'
                return result

            logger.info(f"[{sku}] 促销阻止改价, 尝试临时删除促销...")

        except Exception as e:
            result['error'] = f'Initial price update exception: {e}'
            return result

        # Step 2: 促销阻止 → 获取活跃促销信息
        try:
            promos = self.get_active_promotions_for_listing(listing_id)
            if not promos:
                result['error'] = 'Promotion blocking detected but no active markdown promotions found'
                return result

            # 保存促销参数, 用于后续恢复
            saved_promos = []
            for p in promos:
                pid = p.get('promotionId', '')
                ptype = p.get('promotionType', '')
                pname = p.get('name', '')

                # 获取详情以读取折扣百分比
                detail = self._fetch_promotion_detail(pid, ptype)
                disc_pct = self._extract_discount_pct(detail or p)

                saved_promos.append({
                    'promotion_id': pid,
                    'name': pname,
                    'type': ptype,
                    'discount_pct': disc_pct,
                    'start_date': p.get('startDate', ''),
                    'end_date': p.get('endDate', ''),
                })

            logger.info(f"  找到 {len(saved_promos)} 个活跃 Markdown 促销")

        except Exception as e:
            result['error'] = f'Failed to fetch promotions: {e}'
            return result

        # Step 3: 删除促销
        deleted_ids = []
        for sp in saved_promos:
            pid = sp['promotion_id']
            logger.info(f"  删除促销: {sp['name']} (ID={pid}, {sp['discount_pct']}% off)")

            # 尝试 DELETE (Markdown Sale)
            del_result = self.delete_promotion(pid)
            if del_result.get('success'):
                deleted_ids.append(pid)
            else:
                # 如果 DELETE 失败 (某些促销需要用 /promotion/{id}/DELETE 而非 /item_price_markdown)
                alt_url = f"{self.base}/sell/marketing/v1/promotion/{pid}"
                try:
                    r = requests.delete(alt_url, headers=self._headers(),
                                       timeout=30, verify=False)
                    if r.status_code in (200, 204):
                        deleted_ids.append(pid)
                        logger.info(f"  ✅ 促销已删除 (alt endpoint): {pid}")
                    else:
                        logger.warning(f"  ⚠️ 删除促销失败: {pid} ({r.status_code})")
                except Exception as e:
                    logger.warning(f"  ⚠️ 删除促销异常: {pid} ({e})")

        if not deleted_ids:
            result['error'] = 'Failed to delete any blocking promotions'
            return result

        result['promotion_handled'] = True

        # Step 4: 等待 eBay 处理
        logger.info(f"  等待 eBay 处理促销删除...")
        time.sleep(3)

        # Step 5: 重新改价
        try:
            # 重新获取 offer (因为促销删除后 offer 状态可能变化)
            resp = requests.get(offer_url, headers=self._headers(), timeout=30, verify=False)
            if resp.status_code != 200:
                result['error'] = f'Re-GET offer failed after promo delete: {resp.status_code}'
                return result

            offers = resp.json().get('offers', [])
            if not offers:
                result['error'] = 'No offer found after promo delete'
                return result

            offer = offers[0]
            offer_id = offer['offerId']
            offer['pricingSummary']['price']['value'] = str(round(new_price, 2))

            put_url = f"{self.base}/sell/inventory/v1/offer/{offer_id}"
            resp = requests.put(
                put_url,
                headers=self._inventory_headers(),
                json=offer,
                timeout=60,
                verify=False,
            )

            if resp.status_code in (200, 204):
                result['price_updated'] = True
                result['success'] = True
                logger.info(f"  ✅ [{sku}] 价格已更新: ${new_price:.2f}")
            else:
                err_text = resp.text[:200]
                result['error'] = f'Price update after promo delete failed: {resp.status_code} - {err_text}'
                logger.error(f"  ❌ 删除促销后改价仍失败: {resp.status_code} {err_text}")

        except Exception as e:
            result['error'] = f'Price update after promo delete exception: {e}'
            logger.error(f"  ❌ 改价异常: {e}")

        # Step 6: 恢复促销 (无论改价是否成功,都要恢复)
        for sp in saved_promos:
            if sp['promotion_id'] not in deleted_ids:
                continue
            if sp['discount_pct'] <= 0:
                logger.info(f"  促销 {sp['name']} 折扣率为0, 跳过恢复")
                continue

            # 检查结束时间是否已过
            end_str = sp.get('end_date', '')
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                    if end_dt < datetime.now(timezone.utc):
                        logger.info(f"  促销 {sp['name']} 已过期, 跳过恢复")
                        continue
                except Exception:
                    pass

            logger.info(f"  恢复促销: {sp['name']} ({sp['discount_pct']}% off)...")
            try:
                # 解析原始的开始/结束时间
                start_date = None
                if sp.get('start_date'):
                    try:
                        start_date = datetime.fromisoformat(
                            sp['start_date'].replace('Z', '+00:00'))
                    except Exception:
                        pass
                # 使用"现在"作为新的开始时间
                if not start_date or start_date < datetime.now(timezone.utc):
                    start_date = datetime.now(timezone.utc) + timedelta(minutes=1)

                end_date = None
                if sp.get('end_date'):
                    try:
                        end_date = datetime.fromisoformat(
                            sp['end_date'].replace('Z', '+00:00'))
                    except Exception:
                        pass
                if not end_date or end_date < start_date:
                    end_date = start_date + timedelta(days=2)

                restore_name = sp.get('name', 'AquaVerve Sale')
                if not restore_name.endswith('(restored)'):
                    restore_name = f"{restore_name} (restored)"

                create_result = self.create_markdown_sale(
                    name=restore_name,
                    discount_pct=sp['discount_pct'],
                    start_date=start_date,
                    end_date=end_date,
                    auto_select_all=True,
                )
                if create_result.get('success'):
                    result['promotion_restored'] = True
                    logger.info(f"  ✅ 促销已恢复: {restore_name}")
                else:
                    logger.warning(f"  ⚠️ 促销恢复失败: {create_result.get('error', '?')}")
                    logger.info(f"  ℹ️ 请在 Seller Hub 手动重新创建促销: "
                               f"名称={sp['name']}, 折扣={sp['discount_pct']}%")
            except Exception as e:
                logger.warning(f"  ⚠️ 促销恢复异常: {e}")
                logger.info(f"  ℹ️ 请在 Seller Hub 手动重新创建促销: "
                           f"名称={sp['name']}, 折扣={sp['discount_pct']}%")

        return result

    def _save_discount_state(self, promo_data):
        """保存活跃折扣到本地缓存"""
        state = load_discount_state()
        state['active_discount'] = {
            'discount_pct': promo_data.get('discount_pct', 0),
            'promotion_id': promo_data.get('promotion_id', ''),
            'name': promo_data.get('name', ''),
            'start_date': promo_data.get('start_date', ''),
            'end_date': promo_data.get('end_date', ''),
            'listing_count': promo_data.get('listing_count', 0),
            'created_at': datetime.now().isoformat(),
        }
        DISCOUNT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DISCOUNT_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _clear_discount_state(self):
        """清除活跃折扣状态"""
        state = {'active_discount': None, 'updated_at': datetime.now().isoformat()}
        DISCOUNT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DISCOUNT_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════ 折扣状态全局查询 (Dashboard 用) ═══════════════════

def load_discount_state():
    """
    加载当前折扣状态

    Returns:
        dict: {'active_discount': {...} or None}
    """
    if DISCOUNT_CACHE_FILE.exists():
        try:
            with open(DISCOUNT_CACHE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
            # 检查是否过期
            active = state.get('active_discount')
            if active:
                end_str = active.get('end_date', '')
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                        if end_dt < datetime.now(timezone.utc):
                            # 促销已过期
                            state['active_discount'] = None
                    except:
                        pass
            return state
        except:
            pass
    return {'active_discount': None}


def get_active_discount_pct():
    """
    获取当前生效的折扣百分比

    Returns:
        float: 0.0 (无折扣) 或 e.g. 5.0 (5% off)
    """
    state = load_discount_state()
    active = state.get('active_discount')
    if active:
        return float(active.get('discount_pct', 0))
    return 0.0


def calc_discounted_price(original_price, discount_pct=None):
    """
    计算折扣后价格

    Args:
        original_price: 原价
        discount_pct: 折扣百分比 (None = 使用当前活跃折扣)

    Returns:
        float: 折后价
    """
    if discount_pct is None:
        discount_pct = get_active_discount_pct()
    if discount_pct <= 0 or original_price <= 0:
        return original_price
    return original_price * (1 - discount_pct / 100.0)


def calc_net_margin_with_discount(selling_price, total_cost, discount_pct=None,
                                   ebay_fee_rate=0.1325, ad_rate=0.05, fixed_fee=0.30):
    """
    计算折扣后的净利润率

    eBay 费用 = 折后价 × 费率 (eBay 按实际成交价计费)
    广告费 = 折后价 × 竞价率 × 成交量 (按折后价计算)

    Args:
        selling_price: 原始刊登价
        total_cost: 总成本
        discount_pct: 折扣百分比 (None = 使用当前活跃折扣)
    Returns:
        float: 折后净利润率
    """
    if discount_pct is None:
        discount_pct = get_active_discount_pct()

    actual_price = selling_price * (1 - discount_pct / 100.0) if discount_pct > 0 else selling_price

    if actual_price <= 0 or total_cost <= 0:
        return 0

    ebay_fee = actual_price * ebay_fee_rate
    ad_fee = actual_price * ad_rate
    net_profit = actual_price - ebay_fee - ad_fee - fixed_fee - total_cost
    return net_profit / actual_price


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    print("=" * 60)
    print("eBay Discount Service — 状态检查")
    print("=" * 60)

    state = load_discount_state()
    active = state.get('active_discount')
    if active:
        print(f"\n✅ 当前有活跃折扣:")
        print(f"   名称: {active.get('name', '?')}")
        print(f"   折扣: {active.get('discount_pct', '?')}%")
        print(f"   开始: {active.get('start_date', '?')}")
        print(f"   结束: {active.get('end_date', '?')}")
    else:
        print("\n⚪ 当前无活跃折扣")

    # Test eBay API connection
    try:
        svc = EbayDiscountService()
        promos = svc.fetch_promotions('RUNNING')
        print(f"\neBay 活跃促销数: {len(promos)}")
        for p in promos:
            print(f"  {p.get('name', '?')} | {p.get('promotionType', '?')} | "
                  f"{p.get('promotionStatus', '?')}")
    except Exception as e:
        print(f"\n⚠️ eBay API 连接异常: {e}")
