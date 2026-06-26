"""
Sales Health Check — 每日销售健康诊断 + 自动修复

对所有 PUBLISHED 链接进行健康诊断:
1. 高展示低转化 (impressions > 50, sales = 0)  → 建议降价或优化标题
2. 零流量链接 (views = 0 in 30 days)           → 建议标题重写
3. 长期无销售 (> 14 days)                       → 建议 markdown 促销
4. 定价偏高 (比市场均价高 >15%)                  → 建议跟价
5. 优质链接 (转化率 > 3%)                       → 建议增加广告投放

功能:
- 每日自动运行 (由 daily_tasks.py 调用)
- 生成邮件报告 (嵌入每日汇总)
- 可选: 自动执行修复操作 (--auto-fix)

Usage:
    python scripts/sales_health_check.py                    # 仅诊断
    python scripts/sales_health_check.py --email            # 诊断 + 邮件
    python scripts/sales_health_check.py --auto-fix         # 诊断 + 自动修复
    python scripts/sales_health_check.py --auto-fix --email # 全自动
"""

import os
import sys
import json
import re
import time
import logging
import argparse
import sqlite3
import warnings
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from html import escape as html_escape

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from src.utils.report_images import (
    build_thumbnail_img_html,
    make_ebay_listing_url,
    normalize_thumbnail_url,
)

warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / 'logs' / 'sales_health_check.log',
                           encoding='utf-8'),
    ]
)
log = logging.getLogger("health_check")

# ═══════════════════════════════════════════════════════════════
# 阈值配置
# ═══════════════════════════════════════════════════════════════

# 高展示低转化: 展示 > 此值 & 转化 = 0 → 价格或标题有问题
IMPRESSION_THRESHOLD = 50
# 零流量: 30天内 views = 0 → 标题 SEO 差 or 类目不对
ZERO_TRAFFIC_DAYS = 30
# 价格偏高: 比市场均价高于此比例 → 跟价
OVERPRICED_THRESHOLD = 0.15
# 优质链接: 转化率 > 此值 → 加大广告
HIGH_CVR_THRESHOLD = 0.03
# 低转化: 有展示但转化极低
LOW_CVR_THRESHOLD = 0.005
# 降价幅度 (自动修复时)
AUTO_PRICE_CUT_PCT = 5  # 降价 5%
# 最小利润率底线 (不会降到此以下)
MIN_MARGIN = 0.10  # 10%


class SalesHealthChecker:
    """销售健康诊断器"""

    def __init__(self):
        self.db_path = PROJECT_ROOT / 'ebay_collection.db'
        self.published_count = 0
        self.traffic_meta = {
            'api_ok': False,
            'record_count': 0,
            'limit': 0,
            'is_truncated': False,
        }
        self.results = {
            'high_impression_no_sale': [],    # 高展示零转化
            'zero_traffic': [],                # 零流量
            'overpriced': [],                  # 定价偏高
            'high_performers': [],             # 优质链接
            'low_conversion': [],              # 低转化
            'healthy': [],                     # 健康
            'no_data': [],                     # 无数据
            'price_mismatch': [],              # eBay价格 vs DB价格不一致
            'loss_making': [],                 # 亏损产品
            'qty_zero_restocked': [],          # eBay 库存为0但大建有货，已自动恢复
        }
        self.actions_taken = []

    def run(self, auto_fix=False):
        """执行完整健康检查"""
        log.info("=" * 70)
        log.info("SALES HEALTH CHECK — 销售健康诊断")
        log.info("=" * 70)

        # 1. 获取所有 PUBLISHED 产品
        products = self._get_published_products()
        self.published_count = len(products)
        log.info(f"已刊登产品: {len(products)}")

        if not products:
            log.info("没有已刊登的产品")
            return self._build_report()

        # 2. 获取流量 + 销售数据
        traffic_data, sales_data = self._fetch_performance()

        # 3. 获取市场价格数据 (使用缓存)
        market_data = self._load_market_cache()

        # 4. 逐个诊断
        for p in products:
            listing_id = str(p.get('listing_id', ''))
            sku = p['sku']

            traffic = traffic_data.get(listing_id)
            impressions = traffic.get('impressions', 0) if traffic else 0
            views = traffic.get('views', 0) if traffic else 0
            cvr = traffic.get('conversion_rate', 0) if traffic else 0
            transactions = traffic.get('transactions', 0) if traffic else 0

            # Sales by SKU
            sku_sales = sales_data.get('by_sku', {}).get(sku, {})
            total_sold = sku_sales.get('qty', 0)
            total_revenue = sku_sales.get('revenue', 0)

            cost = p.get('total_cost', 0)
            current_price = p.get('selling_price', 0)
            market_avg = market_data.get(p.get('category_id', ''), {}).get('avg_price', 0)

            diagnosis = {
                'sku': sku,
                'title': (p.get('title', ''))[:55],
                'listing_id': listing_id,
                'image_url': p.get('image_url', ''),
                'impressions': impressions,
                'views': views,
                'cvr': round(cvr * 100, 2) if cvr < 1 else round(cvr, 2),
                'transactions': transactions,
                'total_sold': total_sold,
                'current_price': current_price,
                'total_cost': cost,
                'market_avg': market_avg,
                'category_id': p.get('category_id', ''),
                'repriced_at': p.get('repriced_at'),
                'health_repriced_at': p.get('health_repriced_at'),
            }

            # 分类诊断
            if not listing_id:
                diagnosis['issue'] = '无 listing ID'
                diagnosis['recommendation'] = '检查是否刊登成功'
                self.results['no_data'].append(diagnosis)

            elif traffic is None:
                if self.traffic_meta.get('is_truncated'):
                    diagnosis['issue'] = '流量数据未覆盖'
                    diagnosis['recommendation'] = 'Analytics API 仅返回部分链接，本条需人工复核'
                else:
                    diagnosis['issue'] = '流量数据缺失'
                    diagnosis['recommendation'] = '检查 Analytics API 返回与 listing ID 映射'
                self.results['no_data'].append(diagnosis)

            elif impressions == 0 and views == 0:
                diagnosis['issue'] = '30天零流量'
                diagnosis['recommendation'] = '标题重写 + 检查类目'
                self.results['zero_traffic'].append(diagnosis)

            elif impressions >= IMPRESSION_THRESHOLD and transactions == 0 and total_sold == 0:
                diagnosis['issue'] = f'高展示({impressions})零转化'
                # 判断偏贵还是标题问题
                if market_avg > 0 and current_price > market_avg * (1 + OVERPRICED_THRESHOLD):
                    pct_over = ((current_price / market_avg) - 1) * 100
                    diagnosis['recommendation'] = f'定价高于市场{pct_over:.0f}%，建议降价至${market_avg * 0.95:.2f}'
                    diagnosis['issue'] += ' + 定价偏高'
                    self.results['overpriced'].append(diagnosis)
                else:
                    diagnosis['recommendation'] = '优化标题关键词 + 检查首图质量'
                    self.results['high_impression_no_sale'].append(diagnosis)

            elif cvr > 0 and (cvr if cvr < 1 else cvr/100) >= HIGH_CVR_THRESHOLD:
                diagnosis['issue'] = '优质链接'
                diagnosis['recommendation'] = '增加广告竞价 + 保持或适当提价'
                self.results['high_performers'].append(diagnosis)

            elif impressions > 20 and (cvr if cvr < 1 else cvr/100) < LOW_CVR_THRESHOLD and transactions == 0:
                diagnosis['issue'] = '低转化率'
                diagnosis['recommendation'] = '降价5% 或 优化标题/首图'
                self.results['low_conversion'].append(diagnosis)

            else:
                diagnosis['issue'] = '正常'
                diagnosis['recommendation'] = '继续监控'
                self.results['healthy'].append(diagnosis)

        # 5.5 价格完整性检查 (比对 eBay 实际售价 vs DB 建议价格)
        self._check_price_integrity()

        # 5.6 eBay 库存完整性检查 (检测 qty=0 但大建有货的幽灵下架)
        self._check_quantity_integrity(auto_fix=auto_fix)

        # 6. 输出汇总
        self._print_summary()

        # 7. 自动修复 (如果启用)
        if auto_fix:
            self._auto_fix()

        return self._build_report()

    def _get_published_products(self):
        """获取所有 PUBLISHED 产品"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT sku, title, listing_id, cost_breakdown, optimization, images, suggested_price, price
            FROM collected_products
            WHERE status = 'PUBLISHED'
            ORDER BY sku
        """).fetchall()

        products = []
        for r in rows:
            cost = json.loads(r['cost_breakdown']) if r['cost_breakdown'] else {}
            opt = json.loads(r['optimization']) if r['optimization'] else {}

            # 提取第一张图片作为缩略图
            image_url = self._extract_first_image_url(r['images'])

            products.append({
                'sku': r['sku'],
                'title': opt.get('title', '') or r['title'] or '',
                'listing_id': r['listing_id'],
                'total_cost': cost.get('total_dajian_cost', 0),
                'selling_price': (
                    r['suggested_price']
                    or cost.get('selling_price')
                    or r['price']
                    or 0
                ),
                'category_id': str(opt.get('categoryId', '')),
                'image_url': image_url,
                'repriced_at': cost.get('repriced_at'),
                'health_repriced_at': cost.get('health_repriced_at'),
            })

        conn.close()
        return products

    def _fetch_performance(self):
        """获取 eBay 流量和销售数据"""
        try:
            from src.services.ebay_performance import EbayPerformanceService
            perf = EbayPerformanceService()

            log.info("获取流量数据 (30天)...")
            traffic = perf.fetch_traffic_data(days=30)
            self.traffic_meta = getattr(perf, 'last_traffic_fetch_meta', {}) or {}
            log.info(f"  流量数据: {len(traffic)} listings")

            log.info("获取销售数据 (90天)...")
            sales = perf.fetch_sales_data(days=90)
            log.info(f"  销售数据: {sales.get('total_orders', 0)} orders, "
                     f"${sales.get('total_revenue', 0):.2f} revenue")

            return traffic, sales
        except Exception as e:
            log.error(f"获取性能数据失败: {e}")
            self.traffic_meta = {
                'api_ok': False,
                'record_count': 0,
                'limit': 0,
                'is_truncated': False,
            }
            return {}, {'by_sku': {}, 'by_listing': {}, 'total_orders': 0, 'total_revenue': 0}

    def _load_market_cache(self):
        """加载最近的市场价格缓存 (来自 batch_smart_reprice)"""
        cache = {}
        # 查找最近的 reprice report
        report_dir = PROJECT_ROOT / 'reports'
        if not report_dir.exists():
            return cache

        reports = sorted(report_dir.glob('reprice_report_*.json'), reverse=True)
        if not reports:
            return cache

        latest = reports[0]
        try:
            with open(latest, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cache = data.get('market_research', {})
            log.info(f"加载市场价格缓存: {latest.name} ({len(cache)} categories)")
        except Exception as e:
            log.warning(f"加载市场缓存失败: {e}")

        return cache

    @staticmethod
    def _select_best_offer(offers, expected_listing_id=None):
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

    def _fetch_live_offer(self, sku, headers, expected_listing_id=None):
        """Fetch the offer that best matches the current listing."""
        import requests

        try:
            url = f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}"
            resp = requests.get(url, headers=headers, timeout=15, verify=False)
            if resp.status_code != 200:
                return None

            offers = resp.json().get('offers', [])
            return self._select_best_offer(offers, expected_listing_id=expected_listing_id)
        except Exception:
            return None

    def _fetch_trading_listing_available_quantity(self, listing_id: str | None):
        """Fetch available listing quantity from Trading API for cross-checking."""
        if not listing_id:
            return None

        try:
            import xml.etree.ElementTree as ET
            from src.clients.ebay_client import EbayClient
            from src.clients.ebay_trading_client import EbayTradingClient

            ebay = EbayClient(
                os.getenv("EBAY_APP_ID"),
                os.getenv("EBAY_CERT_ID"),
                os.getenv("EBAY_DEV_ID"),
                env="production",
            )
            trading = EbayTradingClient(ebay)
            response = trading.get_item(str(listing_id))

            root = ET.fromstring(response)
            ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
            item = root.find('.//ebay:Item', ns)
            if item is None:
                return None

            available_text = item.findtext('ebay:QuantityAvailable', default='', namespaces=ns)
            if available_text not in (None, ''):
                return int(float(available_text))

            quantity_text = item.findtext('ebay:Quantity', default='', namespaces=ns)
            sold_text = item.findtext('ebay:SellingStatus/ebay:QuantitySold', default='0', namespaces=ns)
            if quantity_text in (None, ''):
                return None

            return max(0, int(float(quantity_text)) - int(float(sold_text or 0)))
        except Exception:
            return None

    def _fetch_trading_active_quantity_map(self) -> dict:
        """Load available quantities from Trading ActiveList in batches."""
        result = {}
        try:
            import xml.etree.ElementTree as ET
            from src.clients.ebay_client import EbayClient
            from src.clients.ebay_trading_client import EbayTradingClient

            ebay = EbayClient(
                os.getenv("EBAY_APP_ID"),
                os.getenv("EBAY_CERT_ID"),
                os.getenv("EBAY_DEV_ID"),
                env="production",
            )
            trading = EbayTradingClient(ebay)
            ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}

            page = 1
            total_pages = 1
            while page <= total_pages:
                response = trading.get_active_listings(page=page, limit=200)
                root = ET.fromstring(response)
                total_pages_text = root.findtext(
                    './/ebay:PaginationResult/ebay:TotalNumberOfPages',
                    default='1',
                    namespaces=ns,
                )
                try:
                    total_pages = max(1, int(total_pages_text or 1))
                except Exception:
                    total_pages = 1

                for item in root.findall('.//ebay:ItemArray/ebay:Item', ns):
                    sku = item.findtext('ebay:SKU', default='', namespaces=ns)
                    if not sku:
                        continue

                    quantity_text = item.findtext('ebay:Quantity', default='0', namespaces=ns)
                    sold_text = item.findtext('ebay:SellingStatus/ebay:QuantitySold', default='0', namespaces=ns)
                    available_text = item.findtext('ebay:QuantityAvailable', default='', namespaces=ns)
                    try:
                        quantity = int(float(quantity_text or 0))
                        sold = int(float(sold_text or 0))
                        available = (
                            int(float(available_text))
                            if available_text not in (None, '')
                            else max(0, quantity - sold)
                        )
                    except Exception:
                        continue

                    result[sku] = {
                        'available': available,
                        'quantity': quantity,
                        'sold': sold,
                        'listing_status': item.findtext(
                            'ebay:SellingStatus/ebay:ListingStatus',
                            default='',
                            namespaces=ns,
                        ),
                    }

                page += 1
                if page <= total_pages:
                    time.sleep(0.2)

            log.info(f"  Trading ActiveList 数量缓存: {len(result)} listings")
        except Exception as e:
            log.warning(f"加载 Trading ActiveList 数量缓存失败: {e}")
        return result

    @staticmethod
    def _normalize_thumbnail_url(url: str) -> str:
        """Shrink signed GigaB2B image URLs to thumbnail-sized variants for reports."""
        return normalize_thumbnail_url(url)

    def _extract_first_image_url(self, raw_images) -> str:
        """Read the first image URL from JSON arrays or plain string fields."""
        if not raw_images:
            return ''

        try:
            images = json.loads(raw_images) if isinstance(raw_images, str) else raw_images
        except Exception:
            images = raw_images

        if isinstance(images, list) and images:
            return self._normalize_thumbnail_url(images[0])
        if isinstance(images, str) and images.strip().startswith('http'):
            return self._normalize_thumbnail_url(images)
        return ''

    @staticmethod
    def _extract_logged_new_price(text: str | None):
        """Parse the latest synced price from inventory_sync_log.new_value text."""
        if not text:
            return None
        match = re.search(r'新售价\$(\d+(?:\.\d+)?)', str(text))
        if not match:
            return None
        try:
            return float(match.group(1))
        except Exception:
            return None

    def _load_latest_price_sync_meta(self, conn: sqlite3.Connection, skus: list[str]) -> dict:
        """Load the latest price_updated sync log per SKU."""
        if not skus:
            return {}

        placeholders = ','.join('?' * len(skus))
        rows = conn.execute(
            f"""
            SELECT l.sku, l.old_value, l.new_value, l.message, l.synced_at
            FROM inventory_sync_log l
            JOIN (
                SELECT sku, MAX(synced_at) AS synced_at
                FROM inventory_sync_log
                WHERE action = 'price_updated'
                  AND sku IN ({placeholders})
                GROUP BY sku
            ) latest
              ON l.sku = latest.sku AND l.synced_at = latest.synced_at
            WHERE l.action = 'price_updated'
            """,
            skus,
        ).fetchall()
        return {row['sku']: dict(row) for row in rows}

    def _load_active_markdown_listing_promotions(self) -> dict:
        """Map listing_id to the active markdown promotion that currently covers it."""
        mapping = {}
        try:
            from src.services.ebay_discount_service import EbayDiscountService

            service = EbayDiscountService()
            promos = service.fetch_promotions('RUNNING,SCHEDULED')
            for promo in promos:
                promo_type = promo.get('promotionType')
                if promo_type not in ('MARKDOWN_SALE', 'ITEM_PRICE_MARKDOWN'):
                    continue

                detail = service._fetch_promotion_detail(promo.get('promotionId', ''), promo_type) or {}
                discount_pct = service._extract_discount_pct(detail or promo)
                promo_meta = {
                    'promotion_id': promo.get('promotionId', ''),
                    'promotion_name': promo.get('name', ''),
                    'promotion_type': promo_type,
                    'discount_pct': discount_pct,
                    'promotion_status': promo.get('promotionStatus', ''),
                }

                for discount in detail.get('selectedInventoryDiscounts', []):
                    criterion = discount.get('inventoryCriterion', {}) or {}
                    for listing_id in criterion.get('listingIds', []) or []:
                        listing_id = str(listing_id or '').strip()
                        if listing_id:
                            mapping[listing_id] = promo_meta
        except Exception as e:
            log.warning(f"加载活跃促销映射失败: {e}")

        return mapping

    def _classify_price_mismatch(self, sku: str, listing_id: str | None, ebay_price: float,
                                 db_price: float, cost_data: dict,
                                 sync_meta: dict | None, promo_meta: dict | None):
        """Classify price mismatches into manual / promotion / legacy-unsynced buckets."""
        local_selling_price = cost_data.get('selling_price') if isinstance(cost_data, dict) else None
        try:
            local_selling_price = float(local_selling_price) if local_selling_price not in (None, '') else None
        except Exception:
            local_selling_price = None

        logged_new_price = self._extract_logged_new_price((sync_meta or {}).get('new_value'))

        if local_selling_price is not None and abs(local_selling_price - db_price) > 0.01:
            return (
                '历史遗留不同步',
                f"本地 selling_price=${local_selling_price:.2f}，但 suggested_price=${db_price:.2f}",
            )

        if logged_new_price is not None and abs(logged_new_price - db_price) > 0.01:
            synced_at = (sync_meta or {}).get('synced_at') or '未知时间'
            return (
                '历史遗留不同步',
                f"同步日志在 {synced_at} 记录新售价 ${logged_new_price:.2f}，但 suggested_price 未同步",
            )

        if promo_meta:
            promo_name = promo_meta.get('promotion_name') or promo_meta.get('promotion_id') or 'Markdown 促销'
            discount_pct = float(promo_meta.get('discount_pct') or 0)
            discount_text = f"{discount_pct:.1f}% off" if discount_pct > 0 else '促销锁价中'
            return (
                '促销改价',
                f"当前链接在促销 {promo_name} 下 ({discount_text})",
            )

        return (
            '人工改价',
            f"未发现促销覆盖，且本地价格字段一致，推定为前台/人工改价 (eBay=${ebay_price:.2f})",
        )

    def _check_price_integrity(self):
        """
        价格完整性检查 — 比对 eBay 实际售价 vs DB 建议价格

        检查逻辑:
        1. eBay 实际价格 vs DB suggested_price 偏差 > 5% → price_mismatch
        2. eBay 实际价格低于成本底价 (扣完费后亏钱) → loss_making
        """
        log.info("\n" + "-" * 50)
        log.info("PRICE INTEGRITY CHECK — 价格完整性检查")
        log.info("-" * 50)

        try:
            from src.services.ebay_auth import EbayOAuthService
            oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
            token = oauth.get_valid_token()
        except Exception as e:
            log.warning(f"无法获取 eBay 令牌, 跳过价格完整性检查: {e}")
            return

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT sku, listing_id, suggested_price, cost_breakdown
            FROM collected_products
            WHERE status = 'PUBLISHED'
              AND cost_breakdown IS NOT NULL
              AND suggested_price IS NOT NULL
        """).fetchall()

        latest_sync_meta = self._load_latest_price_sync_meta(conn, [r['sku'] for r in rows])
        promo_map = self._load_active_markdown_listing_promotions()

        checked = 0
        mismatches = 0
        losses = 0

        for r in rows:
            sku = r['sku']
            listing_id = str(r['listing_id'] or '')
            db_price = float(r['suggested_price'] or 0)
            cost_data = json.loads(r['cost_breakdown']) if r['cost_breakdown'] else {}
            total_cost = cost_data.get('total_dajian_cost', 0) or 0

            if db_price <= 0 or total_cost <= 0:
                continue

            # 获取 eBay 实际价格
            try:
                offer = self._fetch_live_offer(sku, headers, expected_listing_id=r['listing_id'])
                if not offer:
                    continue

                ebay_price = float(offer.get('pricingSummary', {}).get('price', {}).get('value', 0))
                if ebay_price <= 0:
                    continue

                checked += 1

                # 计算实际利润率 (考虑5%店铺折扣)
                worst_net = ebay_price * 0.95 * 0.8175 - 0.30
                actual_margin = (worst_net - total_cost) / worst_net * 100 if worst_net > 0 else -999

                # 检查1: 偏差 > 5%
                diff_pct = abs(ebay_price - db_price) / db_price * 100
                if diff_pct > 5:
                    source_type, source_reason = self._classify_price_mismatch(
                        sku=sku,
                        listing_id=listing_id,
                        ebay_price=ebay_price,
                        db_price=db_price,
                        cost_data=cost_data,
                        sync_meta=latest_sync_meta.get(sku),
                        promo_meta=promo_map.get(listing_id),
                    )
                    mismatches += 1
                    self.results['price_mismatch'].append({
                        'sku': sku,
                        'listing_id': listing_id,
                        'ebay_price': ebay_price,
                        'db_price': db_price,
                        'diff_pct': round(diff_pct, 1),
                        'total_cost': total_cost,
                        'actual_margin': round(actual_margin, 1),
                        'source_type': source_type,
                        'source_reason': source_reason,
                    })

                # 检查2: 实际售价扣完 eBay 费用后是否亏损
                if worst_net < total_cost:
                    losses += 1
                    self.results['loss_making'].append({
                        'sku': sku,
                        'ebay_price': ebay_price,
                        'total_cost': total_cost,
                        'worst_net': round(worst_net, 2),
                        'loss': round(total_cost - worst_net, 2),
                    })

            except Exception:
                continue

            # Rate limit
            if checked % 20 == 0:
                time.sleep(0.5)

        conn.close()

        log.info(f"  已检查: {checked}")
        log.info(f"  价格偏差 >5%: {mismatches}")
        log.info(f"  潜在亏损: {losses}")

        if self.results['price_mismatch']:
            log.warning("价格偏差清单:")
            for m in self.results['price_mismatch'][:10]:
                log.warning(f"  {m['sku']}: eBay=${m['ebay_price']:.2f} vs DB=${m['db_price']:.2f} "
                           f"(偏差{m['diff_pct']}%, 来源={m['source_type']})")

        if self.results['loss_making']:
            log.error("⚠️ 潜在亏损清单:")
            for m in self.results['loss_making'][:10]:
                log.error(f"  {m['sku']}: 售价=${m['ebay_price']:.2f}, 成本=${m['total_cost']:.2f}, "
                         f"最差净收=${m['worst_net']:.2f}, 亏损=${m['loss']:.2f}")

    def _check_quantity_integrity(self, auto_fix=False):
        """
        eBay 库存完整性检查 — 检测 qty=0 但大建有货的产品

        场景: eBay 库存被设为 0 (如手动/eBay系统/同步异常)，
              但大建仍然有货，sync 因 last_action 不是 out_of_stock 而跳过恢复。
        """
        import requests
        log.info("\n" + "-" * 50)
        log.info("QUANTITY INTEGRITY CHECK — 库存完整性检查")
        log.info("-" * 50)

        try:
            from src.services.ebay_auth import EbayOAuthService
            oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
            token = oauth.get_valid_token()
        except Exception as e:
            log.warning(f"无法获取 eBay 令牌, 跳过库存完整性检查: {e}")
            return

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT sku, listing_id FROM collected_products WHERE status = 'PUBLISHED'
        """).fetchall()

        checked = 0
        qty_zero = []
        trading_quantity_map = self._fetch_trading_active_quantity_map()

        for r in rows:
            sku = r['sku']
            listing_id = r['listing_id']
            try:
                trading_state = trading_quantity_map.get(sku) or {}
                qty = None
                trading_available = trading_state.get('available')

                if trading_available is None:
                    url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
                    resp = requests.get(url, headers=headers, timeout=15, verify=False)
                    if resp.status_code != 200:
                        continue
                    item = resp.json()
                    qty = item.get('availability', {}).get('shipToLocationAvailability', {}).get('quantity')
                    if qty == 0:
                        trading_available = self._fetch_trading_listing_available_quantity(listing_id)

                checked += 1

                is_out_of_stock = (
                    qty == 0
                    or trading_available == 0
                )

                if is_out_of_stock:
                    if trading_available is not None and trading_available > 0:
                        log.info(
                            f"  {sku}: Inventory API qty=0, 但 Trading 实际可售数量={trading_available}，跳过幽灵下架恢复"
                        )
                        continue
                    qty_zero.append(sku)

            except Exception:
                continue

            if checked % 30 == 0:
                time.sleep(0.5)

        conn.close()

        log.info(f"  已检查: {checked}")
        log.info(f"  eBay 库存为0: {len(qty_zero)}")

        if not qty_zero:
            return

        # 交叉验证：检查大建是否有货
        try:
            from src.clients.dajian_client import DaJianClient
            client_id = os.getenv("DAJIAN_API_KEY")
            client_secret = os.getenv("DAJIAN_API_SECRET")
            if not client_id or not client_secret:
                log.warning("大建 API 凭证未配置, 跳过交叉验证")
                return
            dajian = DaJianClient(client_id, client_secret)
        except Exception as e:
            log.warning(f"大建客户端初始化失败: {e}")
            return

        from src.plugins.inventory_sync.sync_service import InventorySyncService
        svc = InventorySyncService()

        for sku in qty_zero:
            try:
                # 先检查 eBay offer 状态 — 如果已结束/下架则跳过，不自动恢复
                try:
                    offer_status = svc.check_ebay_listing_status(sku)
                except Exception:
                    offer_status = 'UNKNOWN'

                if offer_status in ('ENDED', 'NOT_FOUND'):
                    log.info(f"  {sku}: eBay offer 状态={offer_status}，跳过恢复 (可能是人工下架)")
                    # 同步 DB 状态
                    try:
                        conn2 = sqlite3.connect(str(self.db_path))
                        conn2.execute(
                            "UPDATE collected_products SET status = 'DELISTED' WHERE sku = ? AND status = 'PUBLISHED'",
                            (sku,))
                        if conn2.total_changes > 0:
                            log.info(f"    → 已将 DB 状态更新为 DELISTED")
                        conn2.commit()
                        conn2.close()
                    except Exception as db_err:
                        log.warning(f"    → DB 状态更新失败: {db_err}")
                    continue

                from src.utils.ebay_quantity import normalize_ebay_listing_quantity

                in_stock, _, _, available_quantity = svc.check_dajian_stock(sku)
                if in_stock:
                    log.warning(f"  {sku}: eBay qty=0 但大建有货! (offer={offer_status})")
                    if auto_fix:
                        success = svc.update_ebay_quantity(
                            sku,
                            normalize_ebay_listing_quantity(available_quantity or 1),
                        )
                        status = '已恢复' if success else '恢复失败'
                        log.info(f"    → 自动恢复库存: {status}")
                    else:
                        status = '待修复'

                    self.results['qty_zero_restocked'].append({
                        'sku': sku,
                        'status': status,
                    })
            except Exception as e:
                log.warning(f"  {sku}: 大建库存检查失败: {e}")

        if self.results['qty_zero_restocked']:
            log.warning(f"⚠️ 发现 {len(self.results['qty_zero_restocked'])} 个幽灵下架产品")

    def _print_summary(self):
        """输出诊断汇总"""
        log.info("\n" + "=" * 70)
        log.info("DIAGNOSIS SUMMARY")
        log.info("=" * 70)

        categories = [
            ('high_impression_no_sale', '🔴 高展示零转化', 'red'),
            ('overpriced', '🟠 定价偏高', 'orange'),
            ('zero_traffic', '⚫ 零流量', 'gray'),
            ('low_conversion', '🟡 低转化', 'yellow'),
            ('loss_making', '💣 潜在亏损', 'red'),
            ('price_mismatch', '⚠️ 价格偏差', 'orange'),
            ('high_performers', '🟢 优质链接', 'green'),
            ('healthy', '✅ 健康', 'green'),
            ('no_data', '⚪ 无数据', 'gray'),
        ]

        for key, label, _ in categories:
            items = self.results[key]
            log.info(f"  {label}: {len(items)}")
            if items and key not in ('healthy',):
                for d in items[:5]:
                    if key == 'price_mismatch':
                        log.info(f"    {d['sku']}: eBay=${d['ebay_price']:.2f} vs DB=${d['db_price']:.2f} "
                                 f"(偏差{d['diff_pct']}%)")
                    elif key == 'loss_making':
                        log.info(f"    {d['sku']}: 售价=${d['ebay_price']:.2f}, 成本=${d['total_cost']:.2f}, "
                                 f"亏损=${d['loss']:.2f}")
                    else:
                        log.info(f"    {d['sku']}: {d['issue']} "
                                 f"(imp={d['impressions']}, views={d['views']}, "
                                 f"cvr={d['cvr']}%, sold={d['total_sold']}, "
                                 f"price=${d['current_price']:.2f})")
                        log.info(f"      → {d['recommendation']}")
                if len(items) > 5:
                    log.info(f"    ... and {len(items) - 5} more")

        problem_count = (len(self.results['high_impression_no_sale'])
                        + len(self.results['overpriced'])
                        + len(self.results['zero_traffic'])
                        + len(self.results['low_conversion'])
                        + len(self.results['loss_making'])
                        + len(self.results['price_mismatch']))

        log.info(f"\n  总计: {self.published_count} 链接, {problem_count} 需关注")
        if self.traffic_meta.get('record_count'):
            coverage = f"{self.traffic_meta.get('record_count', 0)}/{self.published_count or 1}"
            limit_note = " (API coverage limited)" if self.traffic_meta.get('is_truncated') else ""
            log.info(f"  流量覆盖: {coverage}{limit_note}")

    def _auto_fix(self):
        """
        自动修复问题链接

        策略:
        1. 定价偏高: 降价到市场均价 × 0.95 (不低于底价)
        2. 高展示零转化 + 价格偏高: 同上
        3. 低转化: 降价 5%
        """
        log.info("\n" + "=" * 70)
        log.info("AUTO-FIX — 自动修复")
        log.info("=" * 70)

        from src.services.ebay_auth import EbayOAuthService
        oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
        today = datetime.now().date()

        def _repriced_today(d):
            for key in ('repriced_at', 'health_repriced_at'):
                raw = d.get(key)
                if not raw:
                    continue
                try:
                    normalized = str(raw).replace('Z', '+00:00')
                    if datetime.fromisoformat(normalized).date() == today:
                        return True
                except Exception:
                    if str(raw)[:10] == today.isoformat():
                        return True
            return False

        # 合并: 定价偏高 + 部分高展示零转化
        to_reprice = []
        # 折扣后的有效分母: (1-5%折扣) × (1-18.25%eBay费) = 0.95 × 0.8175 = 0.776625
        DISCOUNT_DENOM = 0.95 * 0.8175  # 考虑店铺5%折扣
        FIXED_FEE = 0.30

        for d in self.results['overpriced']:
            if _repriced_today(d):
                log.info(f"  {d['sku']}: 今日已执行过重定价，跳过二次改价")
                continue
            target = d.get('market_avg', 0) * 0.95
            if target > 0:
                floor = (d['total_cost'] * (1 + MIN_MARGIN) + FIXED_FEE) / DISCOUNT_DENOM if d['total_cost'] > 0 else 0
                new_price = max(target, floor)
                if new_price < d['current_price'] * 0.97:  # 至少降 3%
                    to_reprice.append({
                        'sku': d['sku'],
                        'listing_id': d.get('listing_id'),
                        'old_price': d['current_price'],
                        'new_price': round(new_price, 2),
                        'reason': '定价偏高 → 跟价',
                    })

        for d in self.results['low_conversion']:
            if _repriced_today(d):
                log.info(f"  {d['sku']}: 今日已执行过重定价，跳过二次改价")
                continue
            new_price = d['current_price'] * (1 - AUTO_PRICE_CUT_PCT / 100)
            if d['total_cost'] > 0:
                floor = (d['total_cost'] * (1 + MIN_MARGIN) + FIXED_FEE) / DISCOUNT_DENOM
                new_price = max(new_price, floor)
            if new_price < d['current_price'] * 0.99:  # 有实际降幅
                to_reprice.append({
                    'sku': d['sku'],
                    'listing_id': d.get('listing_id'),
                    'old_price': d['current_price'],
                    'new_price': round(new_price, 2),
                    'reason': '低转化 → 降价5%',
                })

        if not to_reprice:
            log.info("没有需要自动修复的链接")
            return

        log.info(f"准备自动调价 {len(to_reprice)} 个链接:")

        # Import reprice function
        sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
        from batch_smart_reprice import update_ebay_price, verify_ebay_price_update

        success = 0
        failed = 0
        for item in to_reprice:
            sku = item['sku']
            new_price = item['new_price']
            log.info(f"  {sku}: ${item['old_price']:.2f} → ${new_price:.2f} ({item['reason']})")

            ok = update_ebay_price(
                oauth,
                sku,
                new_price,
                expected_listing_id=item.get('listing_id'),
            )
            if ok:
                verified, live_price = verify_ebay_price_update(
                    oauth,
                    sku,
                    new_price,
                    expected_listing_id=item.get('listing_id'),
                )
                if verified:
                    success += 1
                    self.actions_taken.append({
                        'sku': sku,
                        'action': 'price_cut',
                        'old_price': item['old_price'],
                        'new_price': new_price,
                        'reason': item['reason'],
                        'status': 'success',
                    })
                    try:
                        conn = sqlite3.connect(str(self.db_path))
                        conn.execute(
                            "UPDATE collected_products SET "
                            "cost_breakdown = json_set(cost_breakdown, '$.selling_price', ?, '$.health_repriced_at', ?), "
                            "suggested_price = ? "
                            "WHERE sku = ?",
                            (new_price, datetime.now().isoformat(), new_price, sku)
                        )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        log.warning(f"  DB update failed: {e}")
                else:
                    failed += 1
                    self.actions_taken.append({
                        'sku': sku,
                        'action': 'price_cut',
                        'reason': item['reason'],
                        'status': 'failed',
                    })
                    log.warning(
                        f"  {sku}: auto-fix repricing not verified on eBay "
                        f"(live={live_price}, expected={new_price:.2f})"
                    )
            else:
                failed += 1
                self.actions_taken.append({
                    'sku': sku,
                    'action': 'price_cut',
                    'reason': item['reason'],
                    'status': 'failed',
                })

            time.sleep(1.5)  # Rate limit

        log.info(f"\n自动修复完成: 成功 {success}, 失败 {failed}")

        # 零流量链接: 标记为需要标题优化 (下次 daily_optimize 时优先处理)
        if self.results['zero_traffic']:
            flagged = 0
            try:
                conn = sqlite3.connect(str(self.db_path))
                for d in self.results['zero_traffic']:
                    sku = d['sku']
                    conn.execute(
                        "UPDATE collected_products SET optimization = "
                        "json_set(optimization, '$.needs_title_refresh', 1, "
                        "'$.health_flag_reason', '零流量-需标题优化', "
                        "'$.health_flagged_at', ?) "
                        "WHERE sku = ?",
                        (datetime.now().isoformat(), sku)
                    )
                    flagged += 1
                conn.commit()
                conn.close()
                log.info(f"已标记 {flagged} 个零流量链接为[需要标题优化]")
                for d in self.results['zero_traffic'][:5]:
                    self.actions_taken.append({
                        'sku': d['sku'],
                        'action': 'flag_title_refresh',
                        'old_price': d['current_price'],
                        'new_price': d['current_price'],
                        'reason': '零流量 → 标记标题优化',
                        'status': 'success',
                    })
            except Exception as e:
                log.warning(f"标记零流量链接失败: {e}")

    def _build_report(self):
        """构建报告数据"""
        problem_count = (len(self.results['high_impression_no_sale'])
                        + len(self.results['overpriced'])
                        + len(self.results['zero_traffic'])
                        + len(self.results['low_conversion'])
                        + len(self.results['loss_making'])
                        + len(self.results['price_mismatch']))

        return {
            'timestamp': datetime.now().isoformat(),
            'summary': {
                'total_listings': self.published_count,
                'problems': problem_count,
                'high_impression_no_sale': len(self.results['high_impression_no_sale']),
                'overpriced': len(self.results['overpriced']),
                'zero_traffic': len(self.results['zero_traffic']),
                'low_conversion': len(self.results['low_conversion']),
                'high_performers': len(self.results['high_performers']),
                'healthy': len(self.results['healthy']),
                'no_data': len(self.results['no_data']),
                'price_mismatch': len(self.results['price_mismatch']),
                'loss_making': len(self.results['loss_making']),
                'qty_zero_restocked': len(self.results['qty_zero_restocked']),
                'traffic_records': self.traffic_meta.get('record_count', 0),
                'traffic_coverage_limited': bool(self.traffic_meta.get('is_truncated')),
            },
            'details': self.results,
            'actions_taken': self.actions_taken,
        }

    def _load_sku_thumbnails(self, skus: list) -> dict:
        """加载 SKU 缩略图 URL"""
        if not skus:
            return {}
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        placeholders = ','.join('?' * len(skus))
        rows = conn.execute(
            f"SELECT sku, images FROM collected_products WHERE sku IN ({placeholders})", skus
        ).fetchall()
        result = {}
        for r in rows:
            image_url = self._extract_first_image_url(r['images'])
            if image_url:
                result[r['sku']] = image_url
        conn.close()
        return result

    def generate_email_html(self):
        """生成邮件用 HTML 片段 (用于嵌入每日汇总邮件)"""
        report = self._build_report()
        s = report['summary']

        def _thumb_html(url: str, width: int = 40, height: int = 40) -> str:
            return build_thumbnail_img_html(url, width=width, height=height)

        def _sku_link(sku: str, listing_id: str | None) -> str:
            ebay_url = make_ebay_listing_url(listing_id)
            label = html_escape(str(sku or ''))
            if ebay_url:
                return f'<a href="{ebay_url}" target="_blank" rel="noopener noreferrer">{label}</a>'
            return label

        def _title_link(title: str, listing_id: str | None) -> str:
            ebay_url = make_ebay_listing_url(listing_id)
            label = html_escape(str(title or ''))
            if ebay_url:
                return f'<a href="{ebay_url}" target="_blank" rel="noopener noreferrer">{label}</a>'
            return label

        # 问题链接明细
        problem_rows = ''
        all_problems = (
            self.results['overpriced']
            + self.results['high_impression_no_sale']
            + self.results['low_conversion']
            + self.results['zero_traffic']
        )
        for d in all_problems[:15]:  # 邮件最多显示 15 条
            issue_color = {
                '定价偏高': '#ff6600',
                '零流量': '#999',
                '低转化': '#cc9900',
            }
            color = '#cc0000'
            for k, c in issue_color.items():
                if k in d.get('issue', ''):
                    color = c
                    break

            thumb_html = _thumb_html(d.get('image_url', ''))
            sku_html = _sku_link(d.get('sku', ''), d.get('listing_id'))
            title_html = _title_link(d.get('title', ''), d.get('listing_id'))

            problem_rows += f"""<tr>
                <td style="padding:5px 8px;border:1px solid #ddd;text-align:center;">{thumb_html}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;">{sku_html}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;max-width:200px;overflow:hidden;text-overflow:ellipsis;">{title_html}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;color:{color};">{d['issue']}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">{d['impressions']}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">{d['views']}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">{d['cvr']}%</td>
                <td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">${d['current_price']:.2f}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;font-size:12px;">{d['recommendation']}</td>
            </tr>"""

        # 优质链接
        top_rows = ''
        for d in self.results['high_performers'][:5]:
            top_rows += f"""<tr style="background:#e8f5e9;">
                <td style="padding:5px 8px;border:1px solid #ddd;">{d['sku']}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;">{d['title']}</td>
                <td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">{d['cvr']}%</td>
                <td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">{d['total_sold']}</td>
            </tr>"""

        # 自动修复结果
        fix_html = ''
        if self.actions_taken:
            fix_rows = ''
            for a in self.actions_taken:
                status_icon = '✅' if a['status'] == 'success' else '❌'
                fix_rows += f"""<tr>
                    <td style="padding:4px 8px;border:1px solid #ddd;">{a['sku']}</td>
                    <td style="padding:4px 8px;border:1px solid #ddd;">${a.get('old_price', 0):.2f} → ${a.get('new_price', 0):.2f}</td>
                    <td style="padding:4px 8px;border:1px solid #ddd;">{a['reason']}</td>
                    <td style="padding:4px 8px;border:1px solid #ddd;text-align:center;">{status_icon}</td>
                </tr>"""
            fix_html = f"""
            <h4 style="color:#1a73e8;">🔧 自动修复结果</h4>
            <table style="border-collapse:collapse;width:100%;font-size:13px;">
                <tr style="background:#e3f2fd;">
                    <th style="padding:5px 8px;border:1px solid #ddd;">SKU</th>
                    <th style="padding:5px 8px;border:1px solid #ddd;">价格调整</th>
                    <th style="padding:5px 8px;border:1px solid #ddd;">原因</th>
                    <th style="padding:5px 8px;border:1px solid #ddd;">状态</th>
                </tr>
                {fix_rows}
            </table>"""

        html = f"""
        <h3>4️⃣ 销售健康诊断</h3>
        <table style="border-collapse:collapse;width:100%;">
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">📦 已刊登链接</td>
                <td style="padding:8px;border:1px solid #ddd;font-weight:bold;">{s['total_listings']}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">📈 流量覆盖</td>
                <td style="padding:8px;border:1px solid #ddd;">{s.get('traffic_records', 0)} / {s['total_listings']}</td>
            </tr>
            <tr style="background:#ffebee;">
                <td style="padding:8px;border:1px solid #ddd;">🔴 需关注</td>
                <td style="padding:8px;border:1px solid #ddd;color:red;font-weight:bold;">{s['problems']}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">  └ 高展示零转化</td>
                <td style="padding:8px;border:1px solid #ddd;">{s['high_impression_no_sale']}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">  └ 定价偏高</td>
                <td style="padding:8px;border:1px solid #ddd;">{s['overpriced']}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">  └ 零流量</td>
                <td style="padding:8px;border:1px solid #ddd;">{s['zero_traffic']}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">  └ 低转化</td>
                <td style="padding:8px;border:1px solid #ddd;">{s['low_conversion']}</td>
            </tr>
            <tr style="background:#ffebee;">
                <td style="padding:8px;border:1px solid #ddd;">  └ 💣 潜在亏损</td>
                <td style="padding:8px;border:1px solid #ddd;color:red;">{s.get('loss_making', 0)}</td>
            </tr>
            <tr style="background:#e8f5e9;">
                <td style="padding:8px;border:1px solid #ddd;">🟢 优质链接</td>
                <td style="padding:8px;border:1px solid #ddd;color:green;font-weight:bold;">{s['high_performers']}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">✅ 健康</td>
                <td style="padding:8px;border:1px solid #ddd;">{s['healthy']}</td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd;">⚪ 无数据/未覆盖</td>
                <td style="padding:8px;border:1px solid #ddd;color:#666;">{s['no_data']}</td>
            </tr>
        </table>
        """

        if s.get('traffic_coverage_limited'):
            html += """
            <p style="color:#666;font-size:12px;margin-top:6px;">
                注: eBay Analytics 本次仅返回部分 listing 流量数据，未返回的链接已归为“无数据/未覆盖”，不会误判为“零流量”。
            </p>
            """

        if problem_rows:
            html += f"""
            <details open>
                <summary style="cursor:pointer;font-weight:bold;margin-top:10px;">
                    ⚠️ 问题链接明细 (Top {min(15, len(all_problems))})
                </summary>
                <table style="border-collapse:collapse;width:100%;font-size:12px;margin-top:5px;">
                    <tr style="background:#fff3e0;">
                        <th style="padding:5px 8px;border:1px solid #ddd;">图片</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">SKU</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">标题</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">问题</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">展示</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">浏览</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">转化率</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">售价</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">建议</th>
                    </tr>
                    {problem_rows}
                </table>
            </details>"""

        if top_rows:
            html += f"""
            <details>
                <summary style="cursor:pointer;font-weight:bold;margin-top:10px;">
                    🌟 优质链接 (可加大投放)
                </summary>
                <table style="border-collapse:collapse;width:100%;font-size:13px;margin-top:5px;">
                    <tr style="background:#e8f5e9;">
                        <th style="padding:5px 8px;border:1px solid #ddd;">SKU</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">标题</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">转化率</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">已售</th>
                    </tr>
                    {top_rows}
                </table>
            </details>"""

        html += fix_html

        # 价格完整性检查结果
        if self.results['loss_making'] or self.results['price_mismatch']:
            integrity_html = '<h4 style="color:#d32f2f;">🔍 价格完整性检查</h4>'

            if self.results['loss_making']:
                loss_skus = [m['sku'] for m in self.results['loss_making'][:10]]
                loss_thumbs = self._load_sku_thumbnails(loss_skus)
                loss_rows = ''
                for m in self.results['loss_making'][:10]:
                    thumb_html = _thumb_html(loss_thumbs.get(m['sku'], ''))
                    sku_html = _sku_link(m['sku'], m.get('listing_id'))
                    loss_rows += f"""<tr style="background:#ffebee;">
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:center;">{thumb_html}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;">{sku_html}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">${m['ebay_price']:.2f}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">${m['total_cost']:.2f}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:right;color:red;font-weight:bold;">-${m['loss']:.2f}</td>
                    </tr>"""
                integrity_html += f"""
                <p style="color:red;font-weight:bold;">💣 潜在亏损产品: {len(self.results['loss_making'])} 个</p>
                <table style="border-collapse:collapse;width:100%;font-size:13px;">
                    <tr style="background:#ffcdd2;">
                        <th style="padding:5px 8px;border:1px solid #ddd;">图片</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">SKU</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">eBay售价</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">总成本</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">亏损金额</th>
                    </tr>{loss_rows}
                </table>"""

            if self.results['price_mismatch']:
                mismatch_skus = [m['sku'] for m in self.results['price_mismatch'][:10]]
                mismatch_thumbs = self._load_sku_thumbnails(mismatch_skus)
                source_counts = defaultdict(int)
                mismatch_rows = ''
                for m in self.results['price_mismatch']:
                    source_counts[m.get('source_type') or '未分类'] += 1

                for m in self.results['price_mismatch'][:10]:
                    thumb_html = _thumb_html(mismatch_thumbs.get(m['sku'], ''))
                    sku_html = _sku_link(m['sku'], m.get('listing_id'))
                    mismatch_rows += f"""<tr>
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:center;">{thumb_html}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;">{sku_html}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:right;white-space:nowrap;">${m['ebay_price']:.2f}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:right;white-space:nowrap;">${m['db_price']:.2f}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;text-align:right;color:#b42318;font-weight:bold;white-space:nowrap;">{m['diff_pct']:.1f}%</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;white-space:nowrap;">{html_escape(str(m.get('source_type', '未分类') or '未分类'))}</td>
                        <td style="padding:4px 8px;border:1px solid #ddd;font-size:12px;line-height:1.35;">{html_escape(str(m.get('source_reason', '') or ''))}</td>
                    </tr>"""

                badges = ''.join(
                    f'<span style="display:inline-block;margin:0 8px 8px 0;padding:4px 10px;border-radius:999px;background:#f8f9fc;border:1px solid #d0d5dd;font-size:12px;">{label} {count}</span>'
                    for label, count in (
                        ('人工改价', source_counts.get('人工改价', 0)),
                        ('促销改价', source_counts.get('促销改价', 0)),
                        ('历史遗留不同步', source_counts.get('历史遗留不同步', 0)),
                    ) if count
                )
                integrity_html += f"""
                <p style="color:#b42318;font-weight:bold;">⚠️ 价格偏差产品: {len(self.results['price_mismatch'])} 个</p>
                <div style="margin:6px 0 8px;">{badges}</div>
                <table style="border-collapse:collapse;width:100%;font-size:12px;">
                    <tr style="background:#fef3f2;">
                        <th style="padding:5px 8px;border:1px solid #ddd;">图片</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">SKU</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">eBay售价</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">DB目标价</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">偏差</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">来源</th>
                        <th style="padding:5px 8px;border:1px solid #ddd;">原因说明</th>
                    </tr>{mismatch_rows}
                </table>"""
            html += integrity_html

        # 幽灵下架恢复
        if self.results['qty_zero_restocked']:
            restock_rows = ''
            rs_skus = [r['sku'] for r in self.results['qty_zero_restocked']]
            rs_thumbs = self._load_sku_thumbnails(rs_skus)
            for r in self.results['qty_zero_restocked']:
                thumb_html = _thumb_html(rs_thumbs.get(r['sku'], ''))
                sku_html = _sku_link(r['sku'], r.get('listing_id'))
                status_icon = '✅' if r['status'] == '已恢复' else '⚠️'
                restock_rows += f"""<tr>
                    <td style="padding:4px 8px;border:1px solid #ddd;text-align:center;">{thumb_html}</td>
                    <td style="padding:4px 8px;border:1px solid #ddd;">{sku_html}</td>
                    <td style="padding:4px 8px;border:1px solid #ddd;text-align:center;">{status_icon} {r['status']}</td>
                </tr>"""
            html += f"""
            <h4 style="color:#1565c0;">🔄 幽灵下架恢复</h4>
            <p>eBay 库存为 0 但大建有货的产品: {len(self.results['qty_zero_restocked'])} 个</p>
            <table style="border-collapse:collapse;width:100%;font-size:13px;">
                <tr style="background:#e3f2fd;">
                    <th style="padding:5px 8px;border:1px solid #ddd;">图片</th>
                    <th style="padding:5px 8px;border:1px solid #ddd;">SKU</th>
                    <th style="padding:5px 8px;border:1px solid #ddd;">状态</th>
                </tr>{restock_rows}
            </table>"""

        return html


def run_health_check(auto_fix=False, send_email=False):
    """主函数"""
    checker = SalesHealthChecker()
    report = checker.run(auto_fix=auto_fix)

    # 保存报告
    report_path = PROJECT_ROOT / 'reports' / f'health_check_{datetime.now().strftime("%Y%m%d_%H%M")}.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"报告已保存: {report_path}")

    # 发送邮件
    if send_email:
        try:
            from src.utils.email_sender import send_email as send_mail
            email_html = checker.generate_email_html()

            s = report['summary']
            subject = (f"{'🔴' if s['problems'] > 5 else '🟡' if s['problems'] > 0 else '🟢'} "
                       f"eBay 销售健康报告: {s['problems']} 问题, "
                       f"{s['high_performers']} 优质")

            full_html = f"""
            <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:800px;">
            <h2 style="color:#1a73e8;">📊 eBay 销售健康报告</h2>
            <p style="color:#666;">检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
            <hr>
            {email_html}
            <hr>
            <p style="color:#999;font-size:12px;">此邮件由 Dajian Listing Tool 自动发送</p>
            </body></html>
            """

            send_mail(
                subject=subject,
                html_body=full_html,
            )
            log.info("📧 健康报告邮件已发送")
        except Exception as e:
            log.warning(f"发送邮件失败: {e}")

    return report, checker


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="eBay Sales Health Check")
    parser.add_argument("--auto-fix", action="store_true",
                       help="Auto-fix: reprice overpriced & low-conversion listings")
    parser.add_argument("--email", action="store_true",
                       help="Send email report")
    args = parser.parse_args()

    run_health_check(auto_fix=args.auto_fix, send_email=args.email)
