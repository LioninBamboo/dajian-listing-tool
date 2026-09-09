"""eBay Inventory API 客户端"""
import time
import os
import base64
from typing import Any
import requests
from tenacity import retry, stop_after_attempt, wait_exponential


class EbayClient:
    """eBay Inventory API 客户端，处理产品刊登"""
    
    # API 端点
    ENDPOINTS = {
        "sandbox": "https://api.sandbox.ebay.com",
        "production": "https://api.ebay.com"
    }
    
    AUTH_ENDPOINTS = {
        "sandbox": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "production": "https://auth.ebay.com/oauth2/authorize"
    }
    
    def __init__(
        self,
        app_id: str,
        cert_id: str,
        dev_id: str,
        env: str = "sandbox"
    ):
        """
        初始化 eBay 客户端
        
        Args:
            app_id: eBay App ID
            cert_id: eBay Cert ID
            dev_id: eBay Dev ID
            env: 环境 ("sandbox" 或 "production")
        """
        self.app_id = app_id
        self.cert_id = cert_id
        self.dev_id = dev_id
        self.env = env
        self.base_url = self.ENDPOINTS[env]
        self.access_token: str | None = None
        self.token_expires_at: float = 0
    
    def get_oauth_token(self) -> str:
        """
        获取 OAuth 2.0 Token
        优先从 SQLite 获取 (EbayOAuthService)，支持自动刷新
        """
        # 0. 检查缓存
        if self.access_token and time.time() < self.token_expires_at:
            return self.access_token

        # 1. 优先从 EbayOAuthService 获取（支持自动刷新）
        try:
            from src.services.ebay_auth import EbayOAuthService
            env_name = "PRODUCTION" if self.env == "production" else "SANDBOX"
            oauth_service = EbayOAuthService(env_name)
            if oauth_service.is_authorized():
                token = oauth_service.get_valid_token()  # 自动刷新过期token
                self.access_token = token
                self.token_expires_at = time.time() + 7000  # 约2小时缓存
                return self.access_token
        except Exception as e:
            print(f"[WARN] EbayOAuthService failed: {e}, falling back to env var")
        
        # 2. 回退：尝试使用环境变量的 Refresh Token
        refresh_token = os.getenv("EBAY_REFRESH_TOKEN")
        
        credentials = f"{self.app_id}:{self.cert_id}"
        encoded = base64.b64encode(credentials.encode()).decode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded}"
        }

        if refresh_token:
            # User Token Flow
            url = f"{self.ENDPOINTS[self.env]}/identity/v1/oauth2/token"
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account https://api.ebay.com/oauth/api_scope/sell.fulfillment"
            }
        else:
            # Client Credentials Flow (Application Token) - 权限有限
            url = f"{self.ENDPOINTS[self.env]}/identity/v1/oauth2/token"
            data = {
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope"
            }
        
        response = requests.post(url, headers=headers, data=data, timeout=30, verify=False)
        
        # 增强错误处理
        if response.status_code != 200:
            print(f"DEBUG: Token Error: {response.text}")
            
        response.raise_for_status()
        
        result = response.json()
        self.access_token = result["access_token"]
        self.token_expires_at = time.time() + int(result.get("expires_in", 3600)) - 300
        
        return self.access_token
    
    def create_or_replace_inventory_item(
        self,
        sku: str,
        product: dict[str, Any]
    ) -> dict[str, Any]:
        """
        创建或更新 Inventory Item
        
        Args:
            sku: 产品 SKU
            product: 产品数据
                {
                    "title": "优化后标题",
                    "description": "HTML 描述",
                    "image_urls": ["url1", ...],
                    "price": 29.99,
                    "quantity": 100,
                    "condition": "NEW",
                    "category_id": "1234"
                }
                
        Returns:
            {"sku": "DJ-12345", "status": "success"}
        """
        endpoint = f"/sell/inventory/v1/inventory_item/{sku}"
        
        payload = {
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": product["quantity"]
                }
            },
            "condition": product.get("condition", "NEW"),
            "product": {
                "title": product["title"],
                "description": product["description"],
                "imageUrls": product["image_urls"][:12],  # eBay 最多 12 张图
                "aspects": {
                    "Brand": ["Unbranded"],  # 需要根据实际情况调整
                    "Type": ["Tool"]
                }
            }
        }
        
        response = self._request("PUT", endpoint, json=payload)
        return {"sku": sku, "status": "success"}
    
    def create_offer(
        self,
        sku: str,
        marketplace_id: str = "EBAY_US",
        price: float = 0.0,
        category_id: str = "1"
    ) -> dict[str, Any]:
        """
        为 Inventory Item 创建 Offer
        
        Args:
            sku: 产品 SKU
            marketplace_id: 市场 ID (EBAY_US, EBAY_UK 等)
            price: 价格
            category_id: eBay 类别 ID
            
        Returns:
            {"offer_id": "12345678", "status": "PUBLISHED"}
        """
        endpoint = "/sell/inventory/v1/offer"
        
        payload = {
            "sku": sku,
            "marketplaceId": marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": 0,  # 从 inventory item 自动获取
            "categoryId": category_id,
            "listingPolicies": {
                "paymentPolicyId": "default",  # 需要预先创建 Policy
                "returnPolicyId": "default",
                "fulfillmentPolicyId": "default"
            },
            "pricingSummary": {
                "price": {
                    "value": str(price),
                    "currency": "USD"
                }
            }
        }
        
        response = self._request("POST", endpoint, json=payload)
        return {
            "offer_id": response.get("offerId"),
            "status": "PUBLISHED"
        }
    
    def publish_offer(self, offer_id: str) -> dict[str, Any]:
        """
        发布 Offer 到 eBay
        
        Args:
            offer_id: Offer ID
            
        Returns:
            {"listing_id": "110xxxxx"}
        """
        endpoint = f"/sell/inventory/v1/offer/{offer_id}/publish"
        response = self._request("POST", endpoint)
        return response
    
    def get_item_id(self, sku: str) -> str | None:
        """
        根据 SKU 查询对应的 eBay ItemID
        
        Args:
            sku: 产品 SKU
            
        Returns:
            eBay ItemID 或 None
        """
        try:
            endpoint = f"/sell/inventory/v1/inventory_item/{sku}"
            response = self._request("GET", endpoint)
            
            # 从 offer 列表获取 listing ID
            offers = response.get("offers", [])
            if offers:
                return offers[0].get("listingId")
            return None
        except Exception:
            return None
    
    def update_inventory(self, sku: str, quantity: int) -> dict[str, Any]:
        """
        更新库存数量
        
        Args:
            sku: 产品 SKU
            quantity: 新库存数量
            
        Returns:
            更新结果
        """
        endpoint = f"/sell/inventory/v1/inventory_item/{sku}"
        
        payload = {
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": quantity
                }
            }
        }
        
        response = self._request("PUT", endpoint, json=payload)
        return {"sku": sku, "quantity": quantity, "status": "updated"}
    
    # @retry(
    #     stop=stop_after_attempt(3),
    #     wait=wait_exponential(multiplier=1, min=2, max=10)
    # )
    def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> dict[str, Any]:
        """
        通用 HTTP 请求封装
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            **kwargs: requests 参数
            
        Returns:
            API 响应数据
        """
        token = self.get_oauth_token()
        
        headers = kwargs.get("headers", {})
        headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Language": "en-US",  # eBay Inventory API 必需
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
        })
        kwargs["headers"] = headers
        
        url = f"{self.base_url}{endpoint}"
        
        try:
            # 临时禁用 SSL 验证以应对代理问题
            if 'verify' not in kwargs:
                kwargs['verify'] = False
                
            response = requests.request(method, url, timeout=30, **kwargs)
            
            # eBay 某些成功操作返回 204 No Content
            if response.status_code == 204:
                return {}
            
            response.raise_for_status()
            return response.json() if response.text else {}
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                # Token 失效，清除并重试
                self.access_token = None
                self.token_expires_at = 0
                raise
            elif e.response.status_code == 429:
                # Rate Limit
                retry_after = int(e.response.headers.get("Retry-After", 60))
                time.sleep(retry_after)
                raise
            else:
                error_detail = e.response.text
                print(f"DEBUG: eBay API Error Full: {error_detail}") 
                raise Exception(f"eBay API 错误: {e.response.status_code} - {error_detail}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"网络请求异常: {str(e)}")
