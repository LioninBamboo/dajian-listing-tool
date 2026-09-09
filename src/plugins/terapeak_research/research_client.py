"""
Terapeak/Market Research API 客户端

eBay 提供以下 API 用于市场研究:
1. Browse API - 搜索商品、获取热门商品
2. Marketplace Insights API - 销售趋势分析 (需要额外权限)
3. Analytics API - 卖家分析数据

注意: 完整的 Terapeak 功能需要 eBay 商业账户和特殊 API 权限
"""
import os
import sys
import json
import time
import logging
import requests
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from requests.exceptions import RequestException, SSLError, Timeout, ConnectionError

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")


logger = logging.getLogger(__name__)


@dataclass
class ProductTrend:
    """产品趋势数据"""
    keyword: str
    category_id: str
    category_name: str
    avg_price: float
    total_listings: int
    sold_count: int = 0
    sell_through_rate: float = 0.0
    competition_level: str = ""  # low, medium, high
    trend_direction: str = ""  # rising, stable, declining
    recommendation: str = ""


@dataclass 
class HotProduct:
    """热销产品"""
    item_id: str
    title: str
    price: float
    category_id: str
    seller_username: str
    condition: str
    image_url: str
    item_url: str
    sold_quantity: int = 0
    watch_count: int = 0


class TerapeakClient:
    """Terapeak/市场研究客户端"""
    TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}
    BROWSE_SEARCH_PATH = "/buy/browse/v1/item_summary/search"
    MARKETPLACE_INSIGHTS_PATHS = (
        "/buy/marketplace_insights/v1_beta/item_sales/search",
        "/buy/marketplace-insights/v1_beta/item_sales/search",
    )
    
    def __init__(self):
        from src.services.ebay_auth import EbayOAuthService
        
        self.oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        self.base_url = "https://api.ebay.com"
        self.marketplace_id = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
        self.sold_lookback_days = max(1, min(int(os.getenv("TERAPEAK_SOLD_LOOKBACK_DAYS", "90")), 90))
        self.market_mode = os.getenv("TERAPEAK_MARKET_MODE", "auto").strip().lower() or "auto"
        self._marketplace_insights_probe = None
        self._application_token = None
        self._application_token_cached_at = None
        
    def _get_headers(self, token: Optional[str] = None) -> Dict:
        """获取 API 请求头"""
        token = token or self.oauth.get_valid_token()
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-EBAY-C-MARKETPLACE-ID': self.marketplace_id
        }

    @staticmethod
    def _sleep_with_backoff(attempt: int, base: float = 1.5, cap: float = 12.0):
        time.sleep(min(cap, base * attempt))

    @classmethod
    def _is_transient_http_error(cls, status_code: int) -> bool:
        return status_code in cls.TRANSIENT_HTTP_STATUS

    @staticmethod
    def _coerce_float(value: Any) -> float:
        try:
            if isinstance(value, dict):
                value = value.get('value')
            if value in (None, ''):
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _calculate_price_stats(cls, prices: List[float]) -> Dict[str, float]:
        valid_prices = [float(p) for p in prices if cls._coerce_float(p) > 0]
        if not valid_prices:
            return {
                'avg_price': 0.0,
                'median_price': 0.0,
                'min_price': 0.0,
                'max_price': 0.0,
                'sample_size': 0,
            }

        sorted_prices = sorted(valid_prices)
        q1 = sorted_prices[len(sorted_prices) // 4]
        q3 = sorted_prices[3 * len(sorted_prices) // 4]
        iqr = q3 - q1
        lower = max(0, q1 - 1.5 * iqr)
        upper = q3 + 1.5 * iqr
        filtered = [p for p in sorted_prices if lower <= p <= upper]
        if len(filtered) < 5:
            filtered = sorted_prices

        mid = len(filtered) // 2
        median = filtered[mid]
        return {
            'avg_price': round(sum(filtered) / len(filtered), 2),
            'median_price': round(median, 2),
            'min_price': round(min(filtered), 2),
            'max_price': round(max(filtered), 2),
            'sample_size': len(filtered),
        }

    @staticmethod
    def _infer_min_price(keywords: str = "") -> float:
        keyword_lower = (keywords or "").lower()
        if any(w in keyword_lower for w in ['sofa', 'sectional', 'couch', 'bed', 'table set', 'island', 'vanity', 'dresser', 'cabinet']):
            return 100
        if any(w in keyword_lower for w in ['chair', 'stool', 'ottoman', 'nightstand', 'bench']):
            return 40
        return 20

    def _get_application_token_cached(self) -> Optional[str]:
        if (
            self._application_token
            and self._application_token_cached_at
            and _utcnow_naive() - self._application_token_cached_at < timedelta(minutes=110)
        ):
            return self._application_token

        try:
            self._application_token = self.oauth.get_application_token()
            self._application_token_cached_at = _utcnow_naive()
            return self._application_token
        except Exception as exc:
            logger.warning("Failed to get application token for Terapeak research: %s", exc)
            return None

    def _get_token_candidates(self, prefer_application: bool = True) -> List[Tuple[str, str]]:
        providers = [
            ('application', self._get_application_token_cached),
            ('user', self.oauth.get_valid_token),
        ]
        if not prefer_application:
            providers.reverse()

        candidates: List[Tuple[str, str]] = []
        for auth_mode, provider in providers:
            try:
                token = provider()
                if token:
                    candidates.append((auth_mode, token))
            except Exception as exc:
                logger.warning("Failed to get %s token for Terapeak research: %s", auth_mode, exc)
        return candidates

    @staticmethod
    def _extract_error(resp: requests.Response, data: Any = None) -> Tuple[Optional[int], str]:
        if isinstance(data, dict):
            errors = data.get('errors') or data.get('error')
            if isinstance(errors, list) and errors:
                first = errors[0] or {}
                return first.get('errorId'), first.get('longMessage') or first.get('message') or resp.text[:200]
            if isinstance(errors, dict):
                return errors.get('errorId'), errors.get('longMessage') or errors.get('message') or resp.text[:200]
        text = (resp.text or '').strip()
        return None, text[:200] if text else f"HTTP {resp.status_code}"

    def _request_json(self, path: str, params: Optional[Dict] = None,
                      prefer_application: bool = True,
                      timeout: int = 30,
                      max_attempts: int = 3) -> Dict:
        from ._cache import get_response_cache

        cache = get_response_cache()
        cache_key = cache._make_key("GET", path, params or {}, self.marketplace_id)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_result = {
            'ok': False,
            'data': None,
            'status_code': None,
            'error': 'no token available',
            'error_id': None,
            'endpoint': path,
            'auth_mode': None,
            'transport_error': False,
        }

        for auth_mode, token in self._get_token_candidates(prefer_application=prefer_application):
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = requests.get(
                        url,
                        headers=self._get_headers(token),
                        params=params,
                        timeout=timeout,
                        verify=False,
                    )

                    data = None
                    try:
                        data = resp.json()
                    except ValueError:
                        data = None

                    error_id, error_message = self._extract_error(resp, data)
                    result = {
                        'ok': resp.status_code == 200,
                        'data': data,
                        'status_code': resp.status_code,
                        'error': None if resp.status_code == 200 else error_message,
                        'error_id': error_id,
                        'endpoint': path,
                        'auth_mode': auth_mode,
                        'transport_error': False,
                    }

                    if resp.status_code == 200:
                        cache.set(cache_key, result)
                        return result

                    last_result = result
                    if self._is_transient_http_error(resp.status_code) and attempt < max_attempts:
                        logger.warning(
                            "Terapeak request retry %s/%s for %s after HTTP %s",
                            attempt,
                            max_attempts,
                            path,
                            resp.status_code,
                        )
                        self._sleep_with_backoff(attempt)
                        continue
                    break
                except (SSLError, Timeout, ConnectionError, RequestException) as exc:
                    last_result = {
                        'ok': False,
                        'data': None,
                        'status_code': None,
                        'error': str(exc),
                        'error_id': None,
                        'endpoint': path,
                        'auth_mode': auth_mode,
                        'transport_error': True,
                    }
                    if attempt < max_attempts:
                        logger.warning(
                            "Terapeak request retry %s/%s for %s after transport error: %s",
                            attempt,
                            max_attempts,
                            path,
                            exc,
                        )
                        self._sleep_with_backoff(attempt)
                        continue
                    break

        return last_result

    @classmethod
    def _extract_items_collection(cls, data: Dict) -> List[Dict]:
        if not isinstance(data, dict):
            return []

        for key in ('itemSales', 'itemSalesSummaries', 'salesHistories', 'itemSummaries', 'results'):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return []

    @classmethod
    def _extract_price_from_item(cls, item: Dict) -> float:
        if not isinstance(item, dict):
            return 0.0

        candidate_keys = (
            'price',
            'lastSoldPrice',
            'soldPrice',
            'currentBidPrice',
            'bidPrice',
            'itemPrice',
        )

        for key in candidate_keys:
            if key in item:
                value = cls._coerce_float(item.get(key))
                if value > 0:
                    return value

        for key, value in item.items():
            if isinstance(value, dict) and 'price' in key.lower():
                coerced = cls._coerce_float(value)
                if coerced > 0:
                    return coerced

        return 0.0

    def _build_browse_params(self, keywords: str, category_id: str = None,
                             limit: int = 50, sort: str = "bestMatch",
                             min_price: float = None,
                             buy_it_now_only: bool = True) -> Dict:
        params = {
            'limit': min(limit, 200),
            'sort': sort,
        }
        if keywords:
            params['q'] = keywords
        if category_id:
            params['category_ids'] = category_id

        filters = []
        if buy_it_now_only:
            filters.append("buyingOptions:{FIXED_PRICE}")
        if min_price:
            filters.append(f"price:[{min_price}..],priceCurrency:USD")
        if filters:
            params['filter'] = ','.join(filters)
        return params

    def _browse_search(self, keywords: str, category_id: str = None,
                       limit: int = 50, sort: str = "bestMatch",
                       min_price: float = None,
                       buy_it_now_only: bool = True) -> Dict:
        params = self._build_browse_params(
            keywords=keywords,
            category_id=category_id,
            limit=limit,
            sort=sort,
            min_price=min_price,
            buy_it_now_only=buy_it_now_only,
        )
        result = self._request_json(self.BROWSE_SEARCH_PATH, params=params, prefer_application=True)
        if not result.get('ok'):
            return {
                'items': [],
                'total': 0,
                'error': result.get('error') or 'browse request failed',
                'status_code': result.get('status_code'),
                'auth_mode': result.get('auth_mode'),
                'endpoint': result.get('endpoint'),
            }

        data = result.get('data') or {}
        items = data.get('itemSummaries', [])
        normalized = [{
            'item_id': item.get('itemId'),
            'title': item.get('title'),
            'price': float(item.get('price', {}).get('value', 0)),
            'currency': item.get('price', {}).get('currency', 'USD'),
            'category_id': item.get('categoryId'),
            'category_path': item.get('categoryPath'),
            'condition': item.get('condition'),
            'seller': item.get('seller', {}).get('username'),
            'image_url': item.get('image', {}).get('imageUrl'),
            'item_url': item.get('itemWebUrl'),
            'buying_options': item.get('buyingOptions', []),
        } for item in items]

        return {
            'items': normalized,
            'total': data.get('total', len(normalized)),
            'error': None,
            'status_code': result.get('status_code'),
            'auth_mode': result.get('auth_mode'),
            'endpoint': result.get('endpoint'),
        }

    def _build_sold_search_params(self, keywords: str, category_id: str = None,
                                  limit: int = 100, min_price: float = None,
                                  sold_days: Optional[int] = None,
                                  buy_it_now_only: bool = True) -> Dict:
        sold_days = max(1, min(sold_days or self.sold_lookback_days, 90))
        end_time = _utcnow_naive()
        start_time = end_time - timedelta(days=sold_days)

        params = {
            'limit': min(limit, 200),
        }
        if keywords:
            params['q'] = keywords
        if category_id:
            params['category_ids'] = category_id

        filters = [f"lastSoldDate:[{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}..{end_time.strftime('%Y-%m-%dT%H:%M:%SZ')}]"]
        if buy_it_now_only:
            filters.append("buyingOptions:{FIXED_PRICE}")
        if min_price:
            filters.append(f"price:[{min_price}..],priceCurrency:USD")
        params['filter'] = ','.join(filters)
        return params

    def check_marketplace_insights_access(self, category_id: str = "118218", keywords: str = "accent chair") -> Dict:
        if self._marketplace_insights_probe is not None:
            return dict(self._marketplace_insights_probe)

        params = self._build_sold_search_params(
            keywords=keywords,
            category_id=category_id,
            limit=1,
            min_price=self._infer_min_price(keywords),
            sold_days=30,
        )

        last_result = None
        for path in self.MARKETPLACE_INSIGHTS_PATHS:
            result = self._request_json(path, params=params, prefer_application=True)
            last_result = result
            if result.get('ok'):
                self._marketplace_insights_probe = {
                    'available': True,
                    'reason': 'ok',
                    'status_code': result.get('status_code'),
                    'auth_mode': result.get('auth_mode'),
                    'endpoint': result.get('endpoint'),
                    'error': None,
                }
                return dict(self._marketplace_insights_probe)
            if result.get('status_code') != 404:
                break

        error_text = (last_result or {}).get('error') or 'marketplace insights unavailable'
        reason = 'unavailable'
        if (last_result or {}).get('transport_error'):
            reason = 'transport_error'
        elif (last_result or {}).get('status_code') == 404:
            reason = 'endpoint_not_found'
        elif (last_result or {}).get('error_id') == 100017 or 'insufficient permission' in error_text.lower():
            reason = 'permission_denied'
        elif (last_result or {}).get('status_code') in (401, 403):
            reason = 'permission_denied'

        self._marketplace_insights_probe = {
            'available': False,
            'reason': reason,
            'status_code': (last_result or {}).get('status_code'),
            'auth_mode': (last_result or {}).get('auth_mode'),
            'endpoint': (last_result or {}).get('endpoint'),
            'error': error_text,
        }
        return dict(self._marketplace_insights_probe)

    def search_sold_items(self, keywords: str, category_id: str = None,
                          limit: int = 100, min_price: float = None,
                          sold_days: Optional[int] = None,
                          buy_it_now_only: bool = True) -> Dict:
        params = self._build_sold_search_params(
            keywords=keywords,
            category_id=category_id,
            limit=limit,
            min_price=min_price,
            sold_days=sold_days,
            buy_it_now_only=buy_it_now_only,
        )

        last_result = None
        for path in self.MARKETPLACE_INSIGHTS_PATHS:
            result = self._request_json(path, params=params, prefer_application=True)
            last_result = result
            if result.get('ok'):
                data = result.get('data') or {}
                items = self._extract_items_collection(data)
                normalized = []
                for item in items:
                    normalized.append({
                        'item_id': item.get('itemId') or item.get('legacyItemId'),
                        'title': item.get('title'),
                        'price': self._extract_price_from_item(item),
                        'last_sold_date': item.get('lastSoldDate'),
                        'condition': item.get('condition'),
                        'seller': (item.get('seller') or {}).get('username'),
                        'item_url': item.get('itemWebUrl'),
                        'raw': item,
                    })

                return {
                    'items': normalized,
                    'total': data.get('total', data.get('totalCount', len(normalized))),
                    'error': None,
                    'status_code': result.get('status_code'),
                    'auth_mode': result.get('auth_mode'),
                    'endpoint': result.get('endpoint'),
                    'source': 'MARKETPLACE_INSIGHTS',
                }
            if result.get('status_code') != 404:
                break

        return {
            'items': [],
            'total': 0,
            'error': (last_result or {}).get('error') or 'marketplace insights request failed',
            'status_code': (last_result or {}).get('status_code'),
            'auth_mode': (last_result or {}).get('auth_mode'),
            'endpoint': (last_result or {}).get('endpoint'),
            'source': 'MARKETPLACE_INSIGHTS',
        }

    def get_market_price_snapshot(self, keywords: str, category_id: str = None,
                                  min_price: float = None,
                                  sold_days: Optional[int] = None,
                                  buy_it_now_only: bool = True) -> Dict:
        min_price = min_price if min_price is not None else self._infer_min_price(keywords)
        market_mode = self.market_mode
        if market_mode not in {'auto', 'sold_only', 'browse_only'}:
            market_mode = 'auto'

        insights_probe = None
        if market_mode != 'browse_only':
            insights_probe = self.check_marketplace_insights_access(
                category_id=category_id or '',
                keywords=keywords or '',
            )
            if insights_probe.get('available'):
                sold = self.search_sold_items(
                    keywords=keywords,
                    category_id=category_id,
                    limit=200,
                    min_price=min_price,
                    sold_days=sold_days,
                    buy_it_now_only=buy_it_now_only,
                )
                sold_prices = [item.get('price', 0) for item in sold.get('items', []) if item.get('price', 0) > 0]
                if sold_prices:
                    stats = self._calculate_price_stats(sold_prices)
                    return {
                        **stats,
                        'total_listings': sold.get('total', len(sold_prices)),
                        'sold_count': sold.get('total', len(sold_prices)),
                        'lookback_days': sold_days or self.sold_lookback_days,
                        'source': 'MARKETPLACE_INSIGHTS',
                        'source_label': 'sold-data',
                        'auth_mode': sold.get('auth_mode'),
                        'endpoint': sold.get('endpoint'),
                        'fallback_reason': None,
                    }
                if market_mode == 'sold_only':
                    return {
                        'avg_price': 0,
                        'median_price': 0,
                        'min_price': 0,
                        'max_price': 0,
                        'sample_size': 0,
                        'total_listings': 0,
                        'sold_count': 0,
                        'lookback_days': sold_days or self.sold_lookback_days,
                        'source': 'MARKETPLACE_INSIGHTS',
                        'source_label': 'sold-data',
                        'auth_mode': sold.get('auth_mode'),
                        'endpoint': sold.get('endpoint'),
                        'fallback_reason': 'sold_data_empty',
                        'error': sold.get('error') or 'sold-data returned no usable prices',
                    }
            elif market_mode == 'sold_only':
                return {
                    'avg_price': 0,
                    'median_price': 0,
                    'min_price': 0,
                    'max_price': 0,
                    'sample_size': 0,
                    'total_listings': 0,
                    'sold_count': 0,
                    'lookback_days': sold_days or self.sold_lookback_days,
                    'source': 'MARKETPLACE_INSIGHTS',
                    'source_label': 'sold-data',
                    'auth_mode': insights_probe.get('auth_mode'),
                    'endpoint': insights_probe.get('endpoint'),
                    'fallback_reason': insights_probe.get('reason'),
                    'error': insights_probe.get('error'),
                }

        browse = self._browse_search(
            keywords=keywords,
            category_id=category_id,
            limit=100,
            sort="bestMatch",
            min_price=min_price,
            buy_it_now_only=buy_it_now_only,
        )
        browse_prices = [item.get('price', 0) for item in browse.get('items', []) if item.get('price', 0) > 0]
        stats = self._calculate_price_stats(browse_prices)
        error = browse.get('error')
        if not browse_prices and insights_probe and not error:
            error = insights_probe.get('error')

        return {
            **stats,
            'total_listings': browse.get('total', len(browse_prices)),
            'sold_count': 0,
            'lookback_days': sold_days or self.sold_lookback_days,
            'source': 'BROWSE_FALLBACK',
            'source_label': 'browse-fallback',
            'auth_mode': browse.get('auth_mode'),
            'endpoint': browse.get('endpoint'),
            'fallback_reason': None if market_mode == 'browse_only' else (insights_probe or {}).get('reason'),
            'error': error,
        }
    
    def search_products(self, keywords: str, category_id: str = None, 
                       limit: int = 50, sort: str = "bestMatch",
                       min_price: float = None, buy_it_now_only: bool = True) -> List[Dict]:
        """
        搜索产品 (使用 Browse API)
        
        Args:
            keywords: 搜索关键词
            category_id: 分类 ID (可选)
            limit: 返回数量
            sort: 排序方式 (bestMatch, price, -price, newlyListed)
            min_price: 最低价过滤（过滤虚假低价）
            buy_it_now_only: 只搜索固定价格商品（排除拍卖）
        
        Returns:
            产品列表
        """
        result = self._browse_search(
            keywords=keywords,
            category_id=category_id,
            limit=limit,
            sort=sort,
            min_price=min_price,
            buy_it_now_only=buy_it_now_only,
        )
        if result.get('error'):
            print(f"Search error: {result['error']}")
        return result.get('items', [])

    def search_products_with_total(self, keywords: str, category_id: str = None,
                                   limit: int = 50, sort: str = "bestMatch",
                                   min_price: float = None,
                                   buy_it_now_only: bool = True) -> Dict:
        """
        Same as search_products() but also returns eBay's reported `total`
        active listings so callers can do real competition sizing instead
        of using the per-page sample length.
        """
        result = self._browse_search(
            keywords=keywords,
            category_id=category_id,
            limit=limit,
            sort=sort,
            min_price=min_price,
            buy_it_now_only=buy_it_now_only,
        )
        return {
            'items': result.get('items', []),
            'total': result.get('total', 0),
            'error': result.get('error'),
            'status_code': result.get('status_code'),
        }
    
    def get_category_trends(self, category_id: str) -> Dict:
        """
        获取分类趋势 (使用 Browse API 统计)
        
        由于 Marketplace Insights API 需要特殊权限,
        这里通过搜索 API 进行统计分析
        """
        market = self.get_market_price_snapshot(
            keywords='',
            category_id=category_id,
            min_price=0,
            sold_days=self.sold_lookback_days,
            buy_it_now_only=True,
        )
        if market.get('avg_price', 0) <= 0:
            return {'error': market.get('error') or 'No items found', 'source': market.get('source')}

        return {
            'category_id': category_id,
            'total_listings': market.get('total_listings', 0),
            'sample_size': market.get('sample_size', 0),
            'avg_price': market.get('avg_price', 0),
            'min_price': market.get('min_price', 0),
            'max_price': market.get('max_price', 0),
            'price_median': market.get('median_price', 0),
            'source': market.get('source'),
            'fallback_reason': market.get('fallback_reason'),
        }
    
    def analyze_competition(self, keywords: str, category_id: str = None) -> Dict:
        """
        分析竞争程度
        
        Returns:
            竞争分析结果
        """
        items = self.search_products(keywords, category_id, limit=100)
        
        if not items:
            return {'level': 'unknown', 'reason': 'No data'}
        
        total_listings = len(items)
        prices = [item['price'] for item in items if item['price'] > 0]
        
        # 分析价格分散度
        if prices:
            avg_price = sum(prices) / len(prices)
            price_variance = sum((p - avg_price) ** 2 for p in prices) / len(prices)
            price_std = price_variance ** 0.5
            price_cv = price_std / avg_price if avg_price > 0 else 0  # 变异系数
        else:
            avg_price = 0
            price_cv = 0
        
        # 判断竞争程度
        if total_listings < 100:
            level = 'low'
            recommendation = '竞争较小，建议进入'
        elif total_listings < 1000:
            level = 'medium'
            recommendation = '竞争适中，需差异化定位'
        else:
            level = 'high'
            recommendation = '竞争激烈，需谨慎评估'
        
        return {
            'keywords': keywords,
            'category_id': category_id,
            'total_listings': total_listings,
            'avg_price': round(avg_price, 2),
            'price_range': f'${min(prices):.2f} - ${max(prices):.2f}' if prices else 'N/A',
            'competition_level': level,
            'price_cv': round(price_cv, 2),  # 价格变异系数
            'recommendation': recommendation,
            'analyzed_at': datetime.now().isoformat()
        }
    
    def find_hot_products(self, category_id: str = None, min_price: float = 50, 
                         max_price: float = 500) -> List[HotProduct]:
        """
        发现热销产品
        
        通过价格区间过滤，找到可能的爆品
        """
        # 搜索家具类热门商品
        furniture_keywords = "furniture sofa table chair"
        
        url = f"{self.base_url}/buy/browse/v1/item_summary/search"
        
        filter_str = f"price:[{min_price}..{max_price}],priceCurrency:USD"
        
        params = {
            'q': furniture_keywords,
            'limit': 50,
            'sort': 'price',
            'filter': filter_str
        }
        
        if category_id:
            params['category_ids'] = category_id
        
        try:
            resp = requests.get(url, headers=self._get_headers(), params=params, timeout=30, verify=False)
            resp.raise_for_status()
            
            data = resp.json()
            items = data.get('itemSummaries', [])
            
            hot_products = []
            for item in items:
                hot_products.append(HotProduct(
                    item_id=item.get('itemId', ''),
                    title=item.get('title', ''),
                    price=float(item.get('price', {}).get('value', 0)),
                    category_id=item.get('categoryId', ''),
                    seller_username=item.get('seller', {}).get('username', ''),
                    condition=item.get('condition', ''),
                    image_url=item.get('image', {}).get('imageUrl', ''),
                    item_url=item.get('itemWebUrl', '')
                ))
            
            return hot_products
            
        except Exception as e:
            print(f"Find hot products error: {e}")
            return []


class ProductResearcher:
    """产品调研助手"""
    
    def __init__(self):
        self.terapeak = TerapeakClient()
        
    def research_keyword(self, keyword: str) -> Dict:
        """
        关键词调研
        
        分析关键词的市场潜力
        """
        # 竞争分析
        competition = self.terapeak.analyze_competition(keyword)
        
        # 搜索相关产品
        products = self.terapeak.search_products(keyword, limit=20)
        
        # 价格分析
        prices = [p['price'] for p in products if p['price'] > 0]
        
        return {
            'keyword': keyword,
            'competition': competition,
            'sample_products': products[:5],
            'price_analysis': {
                'avg': round(sum(prices) / len(prices), 2) if prices else 0,
                'min': min(prices) if prices else 0,
                'max': max(prices) if prices else 0,
                'count': len(prices)
            },
            'researched_at': datetime.now().isoformat()
        }
    
    def find_opportunities(self, dajian_products: List[Dict]) -> List[Dict]:
        """
        发现商机
        
        结合大建产品和 eBay 市场数据，发现潜在爆品
        
        Args:
            dajian_products: 大建云仓产品列表
        
        Returns:
            机会列表，按潜力排序
        """
        opportunities = []
        
        for product in dajian_products:
            sku = product.get('sku', '')
            title = product.get('title', '')
            price = product.get('price', 0)
            
            # 提取关键词
            keywords = ' '.join(title.split()[:3])  # 取前3个词
            
            # 市场调研
            competition = self.terapeak.analyze_competition(keywords)
            
            # 计算潜力分数
            score = self._calculate_opportunity_score(
                price=price,
                competition_level=competition.get('competition_level', 'unknown'),
                avg_market_price=competition.get('avg_price', 0)
            )
            
            opportunities.append({
                'sku': sku,
                'title': title,
                'dajian_price': price,
                'market_avg_price': competition.get('avg_price', 0),
                'competition': competition.get('competition_level', 'unknown'),
                'potential_margin': competition.get('avg_price', 0) - price,
                'opportunity_score': score,
                'recommendation': self._get_recommendation(score)
            })
        
        # 按分数排序
        opportunities.sort(key=lambda x: x['opportunity_score'], reverse=True)
        
        return opportunities
    
    def _calculate_opportunity_score(self, price: float, competition_level: str, 
                                    avg_market_price: float) -> int:
        """计算机会分数 (0-100)"""
        score = 50  # 基础分
        
        # 竞争程度加分
        if competition_level == 'low':
            score += 30
        elif competition_level == 'medium':
            score += 15
        elif competition_level == 'high':
            score -= 10
        
        # 利润空间加分
        if avg_market_price > 0 and price > 0:
            margin_rate = (avg_market_price - price) / price
            if margin_rate > 0.5:  # 利润率 > 50%
                score += 20
            elif margin_rate > 0.3:
                score += 10
            elif margin_rate < 0:
                score -= 20
        
        return max(0, min(100, score))
    
    def _get_recommendation(self, score: int) -> str:
        """根据分数给出建议"""
        if score >= 80:
            return "🔥 强烈推荐 - 高潜力爆品"
        elif score >= 60:
            return "✅ 推荐 - 值得尝试"
        elif score >= 40:
            return "⚠️ 谨慎 - 需要进一步调研"
        else:
            return "❌ 不推荐 - 风险较高"
    
    def generate_report(self, keywords: List[str]) -> str:
        """
        生成调研报告
        
        Args:
            keywords: 要调研的关键词列表
        
        Returns:
            Markdown 格式报告
        """
        report_lines = [
            f"# eBay 市场调研报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "---",
            ""
        ]
        
        for kw in keywords:
            result = self.research_keyword(kw)
            
            report_lines.extend([
                f"## 关键词: {kw}",
                "",
                f"### 竞争分析",
                f"- 竞争程度: **{result['competition'].get('competition_level', 'N/A')}**",
                f"- 商品数量: {result['competition'].get('total_listings', 'N/A')}",
                f"- 平均价格: ${result['competition'].get('avg_price', 0):.2f}",
                f"- 价格区间: {result['competition'].get('price_range', 'N/A')}",
                f"- 建议: {result['competition'].get('recommendation', 'N/A')}",
                "",
                f"### 价格统计",
                f"- 样本数: {result['price_analysis']['count']}",
                f"- 平均: ${result['price_analysis']['avg']:.2f}",
                f"- 最低: ${result['price_analysis']['min']:.2f}",
                f"- 最高: ${result['price_analysis']['max']:.2f}",
                "",
                "---",
                ""
            ])
        
        return "\n".join(report_lines)
