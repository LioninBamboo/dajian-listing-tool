"""
大建云仓 Open API 2.0 客户端
基于 HMAC-SHA256 签名认证

API 文档: https://www.gigab2b.com/index.php?route=information/open_api
生产域名: https://openapi.gigab2b.com
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
import time
import hmac
import hashlib
import base64
import json
import logging
import os
import random
import re
import socket
import string
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


def extract_available_inventory_quantity(inventory_info: Optional[Dict]) -> int:
    """Prefer buyer-allocated quantity; fall back to seller-available quantity."""
    if not isinstance(inventory_info, dict):
        return 0

    buyer_inv = inventory_info.get("buyerInventoryInfo") or {}
    seller_inv = inventory_info.get("sellerInventoryInfo") or {}
    quantity = buyer_inv.get("totalBuyerAvailableInventory", 0) or 0
    if not quantity:
        quantity = seller_inv.get("sellerAvailableInventory", 0) or 0
    try:
        return max(0, int(quantity))
    except (TypeError, ValueError):
        return 0


def extract_product_video_urls(detail: Optional[Dict[str, Any]]) -> List[str]:
    """Extract direct product video URLs from a DaJian detail payload."""
    if not isinstance(detail, dict):
        return []

    candidates: List[str] = []

    def _append_candidate(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text:
                candidates.append(text)
            return
        if isinstance(value, dict):
            for key in ("url", "videoUrl", "src"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    candidates.append(nested.strip())
                    return
            return
        if isinstance(value, list):
            for item in value:
                _append_candidate(item)

    _append_candidate(detail.get("productVideoUrl"))
    _append_candidate(detail.get("videoUrls"))

    normalized: List[str] = []
    seen: set[str] = set()
    for raw in candidates:
        url = str(raw or "").strip()
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        dedupe_key = url.split("?", 1)[0]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(url)
    return normalized


def _is_local_proxy_available(proxy_url: str, timeout: float = 0.3) -> bool:
    """Check whether a loopback proxy target is actually listening."""
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port
        if host not in {"127.0.0.1", "localhost", "::1"} or not port:
            return True
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _create_dajian_session(*, retry_transport: bool = True):
    """Create a requests session with retry + verify=False for Dajian API.
    
    注意: urllib3 层已处理重试, _request() 方法不再需要额外重试连接错误。
    backoff_factor=1 → 重试间隔: 1s, 2s, 4s (total ≈ 7s)
    """
    session = requests.Session()
    session.verify = False
    session.trust_env = False
    retry_total = 3 if retry_transport else 0
    retry_strategy = Retry(
        total=retry_total,
        connect=retry_total,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST", "GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class DaJianClient:
    """大建云仓 Open API 2.0 客户端"""
    
    # 正确的生产环境域名
    DEFAULT_BASE_URL = "https://openapi.gigab2b.com"
    
    # API 路径前缀 (根据官方文档)
    API_PREFIX = "/b2b-overseas-api/v1"
    
    def __init__(self, client_id: str, client_secret: str, base_url: str = None):
        """
        初始化客户端
        
        Args:
            client_id: Client ID (API Key)
            client_secret: Client Secret
            base_url: API Base URL (默认: https://openapi.gigab2b.com)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip('/')
        self._validated = False
        self.session = _create_dajian_session()
        
    def _generate_nonce(self, length: int = 10) -> str:
        """生成随机 nonce (10位字符)"""
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
        
    def _generate_signature(self, api_path: str, timestamp: str, nonce: str) -> str:
        """
        生成 HMAC-SHA256 签名
        
        签名规则 (来自官方文档):
        1. msg = clientId + "&" + apiPath + "&" + timestamp + "&" + nonce
        2. key = clientId + "&" + clientSecret + "&" + nonce
        3. sign = base64(hmac_sha256_hex(msg, key))
        """
        msg = f"{self.client_id}&{api_path}&{timestamp}&{nonce}"
        key = f"{self.client_id}&{self.client_secret}&{nonce}"
        
        # HMAC-SHA256 -> hex string -> base64
        hmac_hex = hmac.new(key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
        signature = base64.b64encode(hmac_hex.encode()).decode()
        return signature
    
    def _request(self, method: str, endpoint: str, json_data: Optional[Dict] = None,
                params: Optional[Dict] = None, retries: int = 4, use_prefix: bool = True,
                retry_transport: bool = True) -> Dict:
        """发送 API 请求"""
        # 根据文档，完整路径需要包含 API 前缀
        full_endpoint = f"{self.API_PREFIX}{endpoint}" if use_prefix else endpoint
        url = f"{self.base_url}{full_endpoint}"
        
        timestamp = str(int(time.time() * 1000))
        nonce = self._generate_nonce()
        
        # 使用新的签名算法
        signature = self._generate_signature(full_endpoint, timestamp, nonce)
        
        # 根据文档，header 使用 client-id 格式
        headers = {
            "Content-Type": "application/json",
            "client-id": self.client_id,
            "timestamp": timestamp,
            "nonce": nonce,
            "sign": signature
        }

        # 代理设置（用于 Fake-IP/DNS 劫持场景）
        proxy_url = os.getenv("DAJIAN_PROXY_URL", "").strip()
        proxy_disabled = os.getenv("DAJIAN_PROXY_DISABLE", "").strip().lower() in ["1", "true", "yes"]
        proxies = None
        if proxy_url and not proxy_disabled:
            if _is_local_proxy_available(proxy_url):
                proxies = {"http": proxy_url, "https": proxy_url}
            else:
                logger.warning(
                    "DAJIAN proxy %s is not reachable; bypassing proxy and using direct connection",
                    proxy_url,
                )

        def parse_response(response):
            if response.status_code == 404:
                raise Exception(f"API endpoint not found: {endpoint}")
            response.raise_for_status()
            result = response.json()
            if result.get("code") not in [200, 0, "200", "0", None]:
                error_msg = result.get("msg", result.get("message", "Unknown error"))
                raise Exception(f"API Error: {error_msg}")
            return result.get("data", result)

        request_session = self.session if retry_transport else _create_dajian_session(retry_transport=False)

        def request_direct():
            return parse_response(request_session.request(
                method=method,
                url=url,
                headers=headers,
                json=json_data,
                params=params,
                timeout=30,
                proxies=None,
            ))
        
        try:
            response = request_session.request(
                method=method,
                url=url,
                headers=headers,
                json=json_data,
                params=params,
                timeout=30,
                proxies=proxies
            )
            return parse_response(response)
            
        except requests.exceptions.Timeout as e:
            if proxies and retry_transport:
                logger.warning(
                    "DAJIAN proxy timed out; retrying once with direct connection: %s",
                    e,
                )
                return request_direct()
            # urllib3 已处理连接重试, 这里只重试应用层超时
            if retries > 0:
                time.sleep(2)
                return self._request(
                    method,
                    endpoint,
                    json_data,
                    params,
                    retries - 1,
                    use_prefix,
                    retry_transport,
                )
            raise Exception(f"API Timeout: {url}")
        except requests.exceptions.ProxyError as e:
            if proxies and retry_transport:
                logger.warning("DAJIAN proxy request failed; retrying direct connection: %s", e)
                return request_direct()
            raise Exception(f"Proxy Error: {e}")
        except requests.exceptions.ConnectionError as e:
            # urllib3 Retry 已重试过 — 不再做额外手动重试, 避免指数膨胀
            raise Exception(f"Connection Error (after urllib3 retries): {e}")
        except Exception as e:
            raise
    
    def test_connection(self) -> bool:
        """测试 API 连接"""
        try:
            self.get_product_list(page=1, page_size=100)
            return True
        except Exception as e:
            logger.warning(f"Connection test failed: {e}")
            return False
    
    def get_product_list(self, page: int = 1, page_size: int = 100, 
                        sort: int = 4, **kwargs) -> List[Dict]:
        """
        获取 Buyer 收藏产品列表 (分页)
        
        Args:
            page: 页码，从 1 开始
            page_size: 每页数量，最小 100，最大 1000
            sort: 排序方式 (1=updateTime.asc, 2=updateTime.desc, 
                          3=datePosted.asc, 4=datePosted.desc)
            **kwargs: 其他可选参数 (firstArrivalDate, lastUpdatedAfter, 
                      queryTimeType, startTime, endTime)
        
        Returns:
            产品列表
        """
        # 官方端点: /b2b-overseas-api/v1/buyer/product/skus/v1
        endpoint = "/buyer/product/skus/v1"
        
        # pageSize 最小 100
        if page_size < 100:
            page_size = 100
        
        payload = {
            "page": page,
            "pageSize": page_size,
            "sort": sort
        }
        
        # 添加可选参数
        for key in ['firstArrivalDate', 'lastUpdatedAfter', 'queryTimeType', 'startTime', 'endTime']:
            if key in kwargs and kwargs[key]:
                payload[key] = kwargs[key]
        
        result = self._request("POST", endpoint, json_data=payload)
        
        if isinstance(result, dict):
            return result.get("records", [])
        return []
    
    def get_product_list_with_page_info(self, page: int = 1, page_size: int = 100, 
                                        sort: int = 4, **kwargs) -> Dict:
        """
        获取产品列表（包含分页信息）
        
        Returns:
            {"records": [...], "pageInfo": {...}}
        """
        endpoint = "/buyer/product/skus/v1"
        
        if page_size < 100:
            page_size = 100
        
        payload = {
            "page": page,
            "pageSize": page_size,
            "sort": sort
        }
        
        for key in ['firstArrivalDate', 'lastUpdatedAfter', 'queryTimeType', 'startTime', 'endTime']:
            if key in kwargs and kwargs[key]:
                payload[key] = kwargs[key]
        
        return self._request("POST", endpoint, json_data=payload)
    
    # ==================== 产品详情 API ====================
    
    def get_product_details(self, skus: List[str]) -> List[Dict]:
        """
        批量获取产品详情
        
        API: POST /b2b-overseas-api/v1/buyer/product/detailInfo/v1
        限流: 10秒内20次
        
        Args:
            skus: SKU列表，最多200个
            
        Returns:
            产品详情列表，每个包含:
            - sku: 产品编码
            - productName: 产品名称
            - description: 产品描述
            - category: 分类
            - weight/weightUnit: 重量
            - length/width/height/lengthUnit: 尺寸
            - imageUrls: 图片列表
            - attributes: 属性 (颜色、材质等)
            - skuAvailable: 是否可购买
            - sellerInfo: 卖家信息
        """
        if len(skus) > 200:
            raise ValueError("SKUs list cannot exceed 200 items")
        
        endpoint = "/buyer/product/detailInfo/v1"
        payload = {"skus": skus}
        
        result = self._request("POST", endpoint, json_data=payload)
        return result if isinstance(result, list) else []
    
    def get_product_detail_by_sku(self, sku: str) -> Optional[Dict]:
        """获取单个产品详情"""
        results = self.get_product_details([sku])
        return results[0] if results else None
    
    def get_product_details_by_names(self, product_names: List[str]) -> List[Dict]:
        """
        通过产品名称批量获取详情
        
        Args:
            product_names: 产品名称列表，最多200个
        """
        if len(product_names) > 200:
            raise ValueError("Product names list cannot exceed 200 items")
        
        endpoint = "/buyer/product/detailInfo/v1"
        payload = {"productNames": product_names}
        
        result = self._request("POST", endpoint, json_data=payload)
        return result if isinstance(result, list) else []
    
    # ==================== 产品价格 API ====================
    
    def get_product_prices(self, skus: List[str]) -> List[Dict]:
        """
        批量获取产品价格
        
        API: POST /b2b-overseas-api/v1/buyer/product/price/v1
        限流: 10秒内10次
        
        Args:
            skus: SKU列表，最多200个
            
        Returns:
            价格信息列表，每个包含:
            - sku: 产品编码
            - currency: 货币 (USD)
            - price: 产品原价
            - shippingFee: 运费
            - shippingFeeRange: 运费区间 {minAmount, maxAmount}
            - exclusivePrice: 专享价
            - discountedPrice: 活动折扣价
            - promotionFrom/promotionTo: 促销时间
            - mapPrice: 最低零售价
            - srpPrice: 建议零售价 (SRP)
            - spotPrice: 阶梯价 [{minQuantity, maxQuantity, price}]
            - skuAvailable: 是否可购买
            - sellerInfo: 卖家信息
        """
        if len(skus) > 200:
            raise ValueError("SKUs list cannot exceed 200 items")
        
        endpoint = "/buyer/product/price/v1"
        payload = {"skus": skus}
        
        result = self._request("POST", endpoint, json_data=payload)
        return result if isinstance(result, list) else []
    
    def get_product_price(self, sku: str) -> Optional[Dict]:
        """获取单个产品价格"""
        results = self.get_product_prices([sku])
        return results[0] if results else None
    
    # ==================== 库存查询 API ====================
    
    def get_inventory(self, skus: List[str]) -> List[Dict]:
        """
        批量获取库存信息
        
        API: POST /b2b-overseas-api/v1/buyer/inventory/quantity/v2
        限流: 10秒内10次
        
        Args:
            skus: SKU列表，最多200个
            
        Returns:
            库存信息列表，每个包含:
            - sku: 产品编码
            - buyerInventoryInfo: Buyer库存信息
              - totalBuyerAvailableInventory: Buyer可用库存总量
              - totalMarginInventory: Buyer保证金库存
              - totalFutureInventory: 期货协议库存
              - buyerInventoryDistribution: 按仓库分布
            - sellerInventoryInfo: 平台产品库存信息
              - sellerAvailableInventory: 平台可售库存
              - sellerInventoryDistribution: 按仓库分布 [{warehouseCode, availableQtyMin, availableQtyMax}]
              - nextArrivalInventory: 下次到货信息
        """
        if len(skus) > 200:
            raise ValueError("SKUs list cannot exceed 200 items")
        
        endpoint = "/buyer/inventory/quantity/v2"
        payload = {"skus": skus}
        
        result = self._request("POST", endpoint, json_data=payload)
        return result if isinstance(result, list) else []
    
    def get_inventory_by_sku(self, sku: str) -> Optional[Dict]:
        """获取单个产品库存"""
        results = self.get_inventory([sku])
        return results[0] if results else None

    # ==================== 履约 API (2025-08+) ====================
    # 文档: docs/GIGA_FULFILLMENT_API.md
    # 一件代发由 GIGA 选仓发货，调用方通常无需指定 warehouse。

    WAREHOUSE_ADDRESS_CHUNK = 200
    ORDER_QUERY_CHUNK = 100

    @staticmethod
    def _chunk_list(items: List[Any], size: int) -> List[List[Any]]:
        if size <= 0:
            raise ValueError("chunk size must be positive")
        return [items[i:i + size] for i in range(0, len(items), size)]

    def query_warehouse_addresses(self, warehouse_codes: List[str]) -> List[Dict]:
        """查询仓库地址。

        API: POST /buyer/warehouse/query-address/v1
        限流: 10秒/20次；单次最多 200 个 code。
        """
        codes = [str(c).strip() for c in (warehouse_codes or []) if str(c).strip()]
        if not codes:
            raise ValueError("warehouse_codes is required")

        endpoint = "/buyer/warehouse/query-address/v1"
        out: List[Dict] = []
        for chunk in self._chunk_list(codes, self.WAREHOUSE_ADDRESS_CHUNK):
            result = self._request("POST", endpoint, json_data={"warehouseCodes": chunk})
            if isinstance(result, list):
                out.extend(result)
            elif result:
                out.append(result)
        return out

    def query_order_tracking(self, order_nos: List[str]) -> List[Dict]:
        """查询发货物流（运单号/承运商/发货仓）。

        API: POST /buyer/order/track-no/v1
        限流: 10秒/20次；单次最多 100 个订单号。
        """
        nos = [str(n).strip() for n in (order_nos or []) if str(n).strip()]
        if not nos:
            raise ValueError("order_nos is required")

        endpoint = "/buyer/order/track-no/v1"
        out: List[Dict] = []
        for chunk in self._chunk_list(nos, self.ORDER_QUERY_CHUNK):
            result = self._request("POST", endpoint, json_data={"orderNo": chunk})
            if isinstance(result, list):
                out.extend(result)
            elif result:
                out.append(result)
        return out

    def query_order_status(self, order_nos: List[str]) -> List[Dict]:
        """查询发货订单状态。

        API: POST /buyer/order/status/v1
        限流: 10秒/20次；单次最多 100 个订单号。
        """
        nos = [str(n).strip() for n in (order_nos or []) if str(n).strip()]
        if not nos:
            raise ValueError("order_nos is required")

        endpoint = "/buyer/order/status/v1"
        out: List[Dict] = []
        for chunk in self._chunk_list(nos, self.ORDER_QUERY_CHUNK):
            result = self._request("POST", endpoint, json_data={"orderNo": chunk})
            if isinstance(result, list):
                out.extend(result)
            elif result:
                out.append(result)
        return out

    @staticmethod
    def validate_dropship_payload(payload: Dict[str, Any]) -> List[str]:
        """轻量校验一件代发 payload，返回错误列表（空=通过）。

        一件代发由 GIGA 选仓发货，不要求 warehouseCode。
        """
        errors: List[str] = []
        if not isinstance(payload, dict):
            return ["payload must be a dict"]

        for key in ("orderDate", "orderNo", "shipName", "shipPhone", "shipAddress1",
                    "shipCity", "shipCountry", "shipZipCode"):
            if not str(payload.get(key) or "").strip():
                errors.append(f"missing required field: {key}")

        lines = payload.get("orderLines")
        if not isinstance(lines, list) or not lines:
            errors.append("orderLines must be a non-empty list")
        else:
            for i, line in enumerate(lines):
                if not isinstance(line, dict):
                    errors.append(f"orderLines[{i}] must be an object")
                    continue
                if not str(line.get("sku") or "").strip():
                    errors.append(f"orderLines[{i}].sku is required")
                try:
                    qty = int(line.get("qty"))
                    if qty < 1:
                        errors.append(f"orderLines[{i}].qty must be >= 1")
                except (TypeError, ValueError):
                    errors.append(f"orderLines[{i}].qty must be an integer")
                if line.get("itemPrice") is None or str(line.get("itemPrice")).strip() == "":
                    errors.append(f"orderLines[{i}].itemPrice is required")

        order_no = str(payload.get("orderNo") or "")
        if order_no and not all(c.isalnum() or c in "._-" for c in order_no):
            errors.append("orderNo may only contain letters, digits, . _ -")

        address_text = " ".join(
            str(payload.get(key) or "") for key in ("shipAddress1", "shipAddress2")
        )
        if re.search(r"\bP\.?\s*O\.?\s+BOX\b", address_text, re.IGNORECASE):
            errors.append("PO Box addresses are not supported by GIGA dropship")

        # eBay channel: Item Number + Transaction ID required for defect-rate recognition
        channel = str(payload.get("salesChannel") or "").strip().lower()
        if channel in ("ebay", "e bay"):
            txn = str(payload.get("ebayTransactionID") or "").strip()
            if not txn or not txn.isdigit():
                errors.append(
                    "ebayTransactionID is required for eBay channel "
                    "(must be eBay Transaction ID / Fulfillment lineItemId, pure digits)"
                )
            if isinstance(lines, list):
                for i, line in enumerate(lines):
                    if not isinstance(line, dict):
                        continue
                    item = str(line.get("ebayItemCode") or "").strip()
                    if not item or not item.isdigit():
                        errors.append(
                            f"orderLines[{i}].ebayItemCode is required for eBay channel "
                            f"(must be eBay Item Number / legacyItemId, pure digits)"
                        )

        return errors

    def import_dropship_order(
        self,
        payload: Dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """导入一件代发订单到 GIGA。

        API: POST /buyer/order/dropShip-sync/v1

        GIGA 负责选仓与承运，调用方一般**不需要**传 warehouse。
        dry_run=True 时只校验并返回将发送的 payload，不调 API。
        """
        endpoint = "/buyer/order/dropShip-sync/v1"
        errors = self.validate_dropship_payload(payload)
        if errors:
            raise ValueError("dropship payload invalid: " + "; ".join(errors))

        # Strip internal bookkeeping keys before GIGA call
        api_payload = {
            k: v for k, v in payload.items() if not str(k).startswith("_")
        }

        if dry_run:
            return {
                "dry_run": True,
                "endpoint": f"{self.API_PREFIX}{endpoint}",
                "note": (
                    "GIGA selects warehouse for dropship; warehouseCode not required. "
                    "eBay channel requires ebayItemCode=Item Number and "
                    "ebayTransactionID=Transaction ID (lineItemId)."
                ),
                "payload": api_payload,
                "meta": {k: v for k, v in payload.items() if str(k).startswith("_")},
            }

        # Order import is a non-idempotent write. Never replay it after an
        # ambiguous timeout or connection failure.
        data = self._request(
            "POST",
            endpoint,
            json_data=api_payload,
            retries=0,
            retry_transport=False,
        )
        return {"dry_run": False, "endpoint": f"{self.API_PREFIX}{endpoint}", "data": data}

    @staticmethod
    def validate_pickup_label_payload(payload: Dict[str, Any]) -> List[str]:
        """轻量校验上门取货+label payload。"""
        errors: List[str] = []
        if not isinstance(payload, dict):
            return ["payload must be a dict"]

        for key in ("orderDate", "orderNo", "shipMethod", "salesChannel"):
            if not str(payload.get(key) or "").strip():
                errors.append(f"missing required field: {key}")

        lines = payload.get("orderLines")
        if not isinstance(lines, list) or not lines:
            errors.append("orderLines must be a non-empty list")
        else:
            for i, line in enumerate(lines):
                if not isinstance(line, dict):
                    errors.append(f"orderLines[{i}] must be an object")
                    continue
                if not str(line.get("sku") or "").strip():
                    errors.append(f"orderLines[{i}].sku is required")
                try:
                    qty = int(line.get("qty"))
                    if qty < 1:
                        errors.append(f"orderLines[{i}].qty must be >= 1")
                except (TypeError, ValueError):
                    errors.append(f"orderLines[{i}].qty must be an integer")

        labels = payload.get("labelFile")
        if labels is None or (isinstance(labels, list) and len(labels) == 0) or (
            isinstance(labels, str) and not labels.strip()
        ):
            errors.append("labelFile is required for pickup-label import (base64)")

        return errors

    def import_pickup_label_order(
        self,
        payload: Dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """导入上门取货订单并上传物流 label（美国站）。

        API: POST /buyer/order/pickUpSellLabel-sync/v1
        dry_run=True 时只校验，不调 API。
        """
        endpoint = "/buyer/order/pickUpSellLabel-sync/v1"
        errors = self.validate_pickup_label_payload(payload)
        if errors:
            raise ValueError("pickup-label payload invalid: " + "; ".join(errors))

        if dry_run:
            # Avoid dumping huge base64 in logs by default
            preview = dict(payload)
            lf = preview.get("labelFile")
            if isinstance(lf, list):
                preview["labelFile"] = [f"<base64 len={len(str(x))}>" for x in lf]
            elif isinstance(lf, str) and lf:
                preview["labelFile"] = f"<base64 len={len(lf)}>"
            return {
                "dry_run": True,
                "endpoint": f"{self.API_PREFIX}{endpoint}",
                "payload_preview": preview,
            }

        data = self._request("POST", endpoint, json_data=payload)
        return {"dry_run": False, "endpoint": f"{self.API_PREFIX}{endpoint}", "data": data}
    
    # ==================== 便捷方法 ====================
    
    def get_full_product_info(self, sku: str) -> Dict:
        """
        获取产品完整信息 (详情 + 价格 + 库存)
        
        Returns:
            {
                "detail": {...},
                "price": {...},
                "inventory": {...}
            }
        """
        return {
            "detail": self.get_product_detail_by_sku(sku),
            "price": self.get_product_price(sku),
            "inventory": self.get_inventory_by_sku(sku)
        }
    
    def get_favorites(
        self,
        page: int = 1,
        page_size: int = 200,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        max_pages: int = 10
    ) -> List[Dict]:
        """
        获取收藏的产品列表
        
        使用官方 Buyer 列表接口 (queryTimeType=2=收藏时间)：
        POST /b2b-overseas-api/v1/buyer/product/skus/v1
        
        注意: 该接口只返回收藏夹/国货库存内的产品。
        """
        from datetime import UTC, datetime, timedelta
        
        # 默认时间范围：近两年
        if not end_time:
            end_time = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        if not start_time:
            start_time = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=730)).strftime("%Y-%m-%d %H:%M:%S")
        
        records: List[Dict] = []
        current_page = max(1, page)
        
        for _ in range(max_pages):
            try:
                result = self.get_product_list_with_page_info(
                    page=current_page,
                    page_size=page_size,
                    sort=4,
                    queryTimeType=2,
                    startTime=start_time,
                    endTime=end_time
                )
                
                if isinstance(result, dict):
                    page_records = result.get("records", [])
                    page_info = result.get("pageInfo", {})
                else:
                    page_records = []
                    page_info = {}
                
                if page_records:
                    records.extend(page_records)
                
                total_page = page_info.get("totalPage") or 0
                if total_page and current_page >= total_page:
                    break
                if not page_records:
                    break
                
                current_page += 1
            except Exception as e:
                logger.warning(f"Favorites fetch failed at page {current_page}: {e}")
                break
        
        if not records:
            logger.warning("No favorites returned from buyer product list (queryTimeType=2).")
        
        return records
    
    def search_products(self, keyword: str, page: int = 1, page_size: int = 50) -> List[Dict]:
        """搜索产品"""
        return self.get_product_list(page, page_size, keyword=keyword)
    
    def get_product_detail(self, sku: str) -> Optional[Dict]:
        """获取产品详情 (通过官方 detailInfo API)
        
        注意: 旧版 /product/detail/{sku} 端点已废弃，
        统一使用 get_product_detail_by_sku (官方 v1 API)。
        """
        return self.get_product_detail_by_sku(sku)
    
    def get_categories(self) -> List[Dict]:
        """获取产品分类列表"""
        try:
            endpoint = "/category/list"
            result = self._request("GET", endpoint)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning(f"Failed to get categories: {e}")
            return []
    
    def get_stock_info(self, sku: str) -> Dict:
        """
        获取库存信息 (使用官方 API)
        
        Returns:
            {"in_stock": bool, "quantity": int, "price": float, "shipping_fee": float, "total_cost": float}
        """
        # 使用官方价格 API 获取价格和是否可购买
        price_info = self.get_product_price(sku)
        if not price_info:
            return {"in_stock": False, "quantity": 0, "price": 0, "error": "Product not found in price API"}
        
        available = price_info.get("skuAvailable", False)
        price = float(price_info.get("price", 0) or 0)
        shipping = float(price_info.get("shippingFee", 0) or 0)
        
        # 使用官方库存 API 获取数量
        qty = 0
        try:
            inv_info = self.get_inventory_by_sku(sku)
            if inv_info:
                qty = extract_available_inventory_quantity(inv_info)
        except Exception as e:
            logger.warning(f"Failed to get inventory for {sku}: {e}")
        
        in_stock = available and qty > 0
        
        return {
            "in_stock": in_stock,
            "quantity": qty,
            "price": price,
            "shipping_fee": shipping,
            "total_cost": price + shipping
        }
