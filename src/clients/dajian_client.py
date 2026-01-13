"""
大建云仓 Open API 2.0 客户端
基于 HMAC-SHA256 签名认证
"""
import requests
import time
import hmac
import hashlib
import base64
from typing import Dict, List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential


class DaJianClient:
    """大建云仓 Open API 2.0 客户端"""
    
    def __init__(self, client_id: str, client_secret: str, base_url: str = "https://openapi.giga2b.com"):
        """
        初始化客户端
        
        Args:
            client_id: Client ID (API Key)
            client_secret: Client Secret
            base_url: API Base URL (生产环境: https://openapi.giga2b.com)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip('/')
        
    def _generate_signature(self, api_path: str, timestamp: str, nonce: str, request_body: str = "") -> str:
        """
        生成 HMAC-SHA256 签名
        
        签名规则:
        1. 拼接字符串: Client ID + API路径 + timestamp + nonce + 请求体
        2. 使用 Client Secret 作为密钥进行 HMAC-SHA256 加密
        3. Base64 编码
        """
        # 构建待签名字符串
        sign_string = f"{self.client_id}{api_path}{timestamp}{nonce}{request_body}"
        
        # HMAC-SHA256 加密
        hmac_obj = hmac.new(
            self.client_secret.encode('utf-8'),
            sign_string.encode('utf-8'),
            hashlib.sha256
        )
        
        # Base64 编码
        signature = base64.b64encode(hmac_obj.digest()).decode('utf-8')
        return signature
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _request(self, method: str, endpoint: str, json_data: Optional[Dict] = None, params: Optional[Dict] = None) -> Dict:
        """
        发送 API 请求
        
        Args:
            method: HTTP 方法
            endpoint: API 端点 (如 /product/page)
            json_data: JSON 请求体
            params: URL 参数
        
        Returns:
            API 响应数据
        """
        url = f"{self.base_url}{endpoint}"
        
        # 生成时间戳和随机数
        timestamp = str(int(time.time() * 1000))  # 毫秒级时间戳
        nonce = str(int(time.time() * 1000000))  # 随机数
        
        # 构建请求体字符串 (用于签名)
        request_body = ""
        if json_data:
            import json
            request_body = json.dumps(json_data, separators=(',', ':'), ensure_ascii=False)
        
        # 生成签名
        signature = self._generate_signature(endpoint, timestamp, nonce, request_body)
        
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "clientId": self.client_id,
            "timestamp": timestamp,
            "nonce": nonce,
            "sign": signature
        }
        
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=json_data,
                params=params,
                timeout=30,
                verify=False  # 如果有 SSL 问题可以禁用验证
            )
            
            response.raise_for_status()
            result = response.json()
            
            # 检查业务状态码
            if result.get("code") != 200:
                error_msg = result.get("msg", "Unknown error")
                raise Exception(f"API Error: {error_msg} (code: {result.get('code')})")
            
            return result.get("data", {})
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network Exception: {str(e)}")
    
    def get_product_list(self, page: int = 1, page_size: int = 50) -> List[Dict]:
        """
        获取产品列表 (分页)
        
        Args:
            page: 页码 (从1开始)
            page_size: 每页数量
        
        Returns:
            产品列表
        """
        endpoint = "/product/page"
        
        payload = {
            "current": page,
            "size": page_size
        }
        
        result = self._request("POST", endpoint, json_data=payload)
        
        # 返回产品记录列表
        records = result.get("records", [])
        return records
    
    def get_product_detail(self, sku: str) -> Dict:
        """
        获取产品详情
        
        Args:
            sku: 产品 SKU
        
        Returns:
            产品详情
        """
        # 注意: API 2.0 文档中可能没有单独的详情接口
        # 这里先用列表接口模拟，实际使用时需要根据文档调整
        products = self.get_product_list(page=1, page_size=100)
        
        for product in products:
            if product.get("sku") == sku:
                return product
        
        raise Exception(f"Product not found: {sku}")
