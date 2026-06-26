"""
eBay 性能数据服务

从 eBay Analytics API (流量) 和 Fulfillment API (订单) 获取实际表现数据:
- 展示量 (impressions)
- 浏览量 (views/clicks)
- 点击率 (CTR)
- 售出数量 (transactions)
- 销售转化率 (conversion rate)
- 实际订单详情
"""
import os
import sys
import json
import logging
import warnings
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")
from src.utils.runtime_cache import get_runtime_cache_path, resolve_runtime_cache_path

logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore')


PERFORMANCE_CACHE_FILE = get_runtime_cache_path('performance_cache.json')


class EbayPerformanceService:
    """eBay 性能数据获取服务"""
    
    METRICS = [
        'LISTING_IMPRESSION_TOTAL',
        'LISTING_VIEWS_TOTAL',
        'CLICK_THROUGH_RATE',
        'SALES_CONVERSION_RATE',
        'TRANSACTION',
    ]
    
    def __init__(self):
        from src.services.ebay_auth import EbayOAuthService
        self.oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
        self.base = self.oauth.api_base
        self.last_traffic_fetch_meta = {
            'api_ok': False,
            'record_count': 0,
            'limit': 0,
            'is_truncated': False,
        }
    
    def _headers(self):
        token = self.oauth.get_valid_token()
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        }

    @staticmethod
    def _format_ebay_utc_timestamp(dt: datetime) -> str:
        """Format timezone-aware UTC timestamps in the shape expected by eBay filters."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
        return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    # ─── Analytics API (Traffic) ───
    
    def fetch_traffic_data(self, days=30):
        """
        从 eBay Analytics API 获取 listing 的流量数据
        
        注意: Analytics API 可能只返回部分 listing 数据。
        未返回的 listing 不能直接视为“零流量”，调用方需要结合
        last_traffic_fetch_meta 判断是否为覆盖不足。
        
        Returns:
            dict: {listing_id: {impressions, views, ctr, conversion_rate, transactions}}
        """
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        start_utc = (now_utc - timedelta(days=days)).replace(hour=0, minute=0, second=0)
        end_date = self._format_ebay_utc_timestamp(now_utc)
        start_date = self._format_ebay_utc_timestamp(start_utc)

        url = f"{self.base}/sell/analytics/v1/traffic_report"
        
        all_records = {}
        limit = 200
        self.last_traffic_fetch_meta = {
            'api_ok': False,
            'record_count': 0,
            'limit': limit,
            'is_truncated': False,
        }

        # eBay API 硬限制: 最多返回 200 条记录, offset 分页无效
        params = {
            'dimension': 'LISTING',
            'filter': f'date_range:[{start_date}..{end_date}]',
            'metric': ','.join(self.METRICS),
            'sort': '-LISTING_IMPRESSION_TOTAL',
            'limit': str(limit),
            'offset': '0',
        }
        
        try:
            logger.info("Fetching Analytics traffic data for UTC range %s -> %s", start_date, end_date)
            r = requests.get(url, headers=self._headers(), params=params, 
                            timeout=60, verify=False)
            if r.status_code != 200:
                logger.error(f"Analytics API error {r.status_code}: {r.text[:300]}")
                return all_records
            
            data = r.json()
            records = data.get('records', [])
            self.last_traffic_fetch_meta = {
                'api_ok': True,
                'record_count': len(records),
                'limit': limit,
                'is_truncated': len(records) >= limit,
            }
            
            # Parse metric order from header
            header_metrics = data.get('header', {}).get('metrics', [])
            metric_keys = [m.get('key', '') for m in header_metrics]
            
            for rec in records:
                dims = rec.get('dimensionValues', [])
                vals = rec.get('metricValues', [])
                listing_id = dims[0].get('value', '') if dims else ''
                if not listing_id:
                    continue
                
                entry = {}
                for i, mv in enumerate(vals):
                    key = metric_keys[i] if i < len(metric_keys) else f'metric_{i}'
                    val = mv.get('value', 0) if mv.get('applicable', False) else 0
                    entry[key] = val
                
                all_records[listing_id] = {
                    'impressions': int(entry.get('LISTING_IMPRESSION_TOTAL', 0)),
                    'views': int(entry.get('LISTING_VIEWS_TOTAL', 0)),
                    'ctr': float(entry.get('CLICK_THROUGH_RATE', 0)),
                    'conversion_rate': float(entry.get('SALES_CONVERSION_RATE', 0)),
                    'transactions': int(entry.get('TRANSACTION', 0)),
                }
            
            logger.info(f"Fetched traffic data: {len(records)} records (API max 200)")
            
        except Exception as e:
            logger.error(f"Analytics API exception: {e}")
        
        logger.info(f"Total traffic records: {len(all_records)}")
        return all_records
    
    # ─── Fulfillment API (Orders/Sales) ───
    
    def fetch_sales_data(self, days=90):
        """
        从 eBay Fulfillment API 获取订单数据，按 listing_id 和 SKU 汇总
        
        Returns:
            dict: {
                'by_listing': {listing_id: {sku, qty, revenue, orders, titles}},
                'by_sku': {sku: {qty, revenue, orders, listing_ids}},
                'total_orders': int,
                'total_revenue': float
            }
        """
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        date_from = self._format_ebay_utc_timestamp(
            (now_utc - timedelta(days=days)).replace(hour=0, minute=0, second=0)
        )
        url = f"{self.base}/sell/fulfillment/v1/order"
        
        all_orders = []
        offset = 0
        limit = 50
        
        while True:
            params = {
                'filter': f'creationdate:[{date_from}..]',
                'limit': str(limit),
                'offset': str(offset),
            }
            
            try:
                r = requests.get(url, headers=self._headers(), params=params,
                                timeout=60, verify=False)
                if r.status_code != 200:
                    logger.error(f"Fulfillment API error {r.status_code}: {r.text[:300]}")
                    break
                
                data = r.json()
                orders = data.get('orders', [])
                total = data.get('total', 0)
                
                all_orders.extend(orders)
                
                if len(all_orders) >= total or len(orders) < limit:
                    break
                offset += limit
                
            except Exception as e:
                logger.error(f"Fulfillment API exception: {e}")
                break
        
        # Aggregate by listing_id and SKU
        by_listing = defaultdict(lambda: {
            'sku': '', 'qty': 0, 'revenue': 0.0, 'orders': 0, 'order_ids': set(), 'titles': set()
        })
        by_sku = defaultdict(lambda: {
            'qty': 0, 'revenue': 0.0, 'orders': 0, 'order_ids': set(), 'listing_ids': set()
        })
        total_revenue = 0.0

        for order in all_orders:
            order_id = order.get('orderId') or order.get('legacyOrderId') or ''
            for li in order.get('lineItems', []):
                listing_id = li.get('legacyItemId', '')
                sku = li.get('sku', '')
                qty = li.get('quantity', 0)
                price = float(li.get('lineItemCost', {}).get('value', 0))
                title = li.get('title', '')[:60]
                
                if listing_id:
                    by_listing[listing_id]['sku'] = sku
                    by_listing[listing_id]['qty'] += qty
                    by_listing[listing_id]['revenue'] += price
                    if order_id:
                        by_listing[listing_id]['order_ids'].add(order_id)
                    by_listing[listing_id]['titles'].add(title)

                if sku:
                    by_sku[sku]['qty'] += qty
                    by_sku[sku]['revenue'] += price
                    if order_id:
                        by_sku[sku]['order_ids'].add(order_id)
                    if listing_id:
                        by_sku[sku]['listing_ids'].add(listing_id)
                
                total_revenue += price
        
        # Convert sets to lists for JSON serialization
        for lid, info in by_listing.items():
            info['orders'] = len(info['order_ids'])
            del info['order_ids']
            info['titles'] = list(info['titles'])
        for sku, info in by_sku.items():
            info['orders'] = len(info['order_ids'])
            del info['order_ids']
            info['listing_ids'] = list(info['listing_ids'])
        
        result = {
            'by_listing': dict(by_listing),
            'by_sku': dict(by_sku),
            'total_orders': len(all_orders),
            'total_revenue': total_revenue,
        }
        
        logger.info(f"Total orders: {len(all_orders)}, unique listings sold: {len(by_listing)}, "
                    f"total revenue: ${total_revenue:.2f}")
        return result
    
    # ─── Combined Performance Data ───
    
    def fetch_all_performance(self, traffic_days=30, sales_days=90):
        """
        获取综合性能数据: 流量 + 销售
        
        Returns:
            dict: {
                'traffic': {listing_id: {impressions, views, ctr, ...}},
                'sales': {by_listing: {...}, by_sku: {...}, ...},
                'fetched_at': ISO timestamp,
                'traffic_days': int,
                'sales_days': int,
            }
        """
        traffic = self.fetch_traffic_data(days=traffic_days)
        sales = self.fetch_sales_data(days=sales_days)
        
        return {
            'traffic': traffic,
            'sales': sales,
            'traffic_meta': dict(self.last_traffic_fetch_meta),
            'fetched_at': datetime.now().isoformat(),
            'traffic_days': traffic_days,
            'sales_days': sales_days,
        }


def get_performance_cache_path():
    """获取性能数据缓存路径"""
    return PERFORMANCE_CACHE_FILE


def save_performance_cache(data):
    """保存性能数据到缓存文件"""
    cache_path = get_performance_cache_path()
    # Convert defaultdicts and sets for JSON
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"Saved performance cache to {cache_path}")


def load_performance_cache(max_age_hours=4):
    """
    从缓存加载性能数据。如果缓存超过 max_age_hours 小时则返回 None
    
    Returns:
        dict or None
    """
    cache_path = resolve_runtime_cache_path(
        'performance_cache.json',
        legacy_filename='_performance_cache.json',
    )
    if not cache_path.exists():
        return None
    
    try:
        with open(cache_path, encoding='utf-8') as f:
            data = json.load(f)
        
        fetched_at = data.get('fetched_at', '')
        if fetched_at:
            dt = datetime.fromisoformat(fetched_at)
            age = datetime.now() - dt
            if age.total_seconds() > max_age_hours * 3600:
                logger.info(f"Performance cache expired ({age})")
                return None
        
        return data
    except Exception as e:
        logger.warning(f"Failed to load performance cache: {e}")
        return None


# ─── CLI: 直接运行获取并缓存数据 ───

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print("=" * 60)
    print("Fetching eBay performance data...")
    print("=" * 60)
    
    svc = EbayPerformanceService()
    data = svc.fetch_all_performance(traffic_days=30, sales_days=90)
    
    # Summary
    traffic = data['traffic']
    sales = data['sales']
    
    print(f"\n--- Traffic (last 30 days) ---")
    print(f"Listings with traffic data: {len(traffic)}")
    if traffic:
        total_imp = sum(v['impressions'] for v in traffic.values())
        total_views = sum(v['views'] for v in traffic.values())
        total_tx = sum(v['transactions'] for v in traffic.values())
        print(f"Total impressions: {total_imp:,}")
        print(f"Total views: {total_views:,}")
        print(f"Total transactions: {total_tx}")
        
        # Top 10
        top = sorted(traffic.items(), key=lambda x: x[1]['impressions'], reverse=True)[:10]
        print(f"\nTop 10 by impressions:")
        for lid, info in top:
            print(f"  {lid}: {info['impressions']:,} imp | {info['views']} views | {info['transactions']} tx | CTR {info['ctr']:.1%}")
    
    print(f"\n--- Sales (last 90 days) ---")
    print(f"Total orders: {sales['total_orders']}")
    print(f"Total revenue: ${sales['total_revenue']:,.2f}")
    print(f"Unique listings sold: {len(sales['by_listing'])}")
    print(f"Unique SKUs sold: {len(sales['by_sku'])}")
    
    if sales['by_sku']:
        print(f"\nSales by SKU:")
        for sku, info in sorted(sales['by_sku'].items(), key=lambda x: x[1]['qty'], reverse=True):
            print(f"  {sku}: {info['qty']} sold, ${info['revenue']:,.2f}")
    
    # Save cache
    save_performance_cache(data)
    print(f"\nCache saved to {get_performance_cache_path()}")
