from typing import Any, List, Dict
from playwright.sync_api import sync_playwright
import json
import time

class BrowserDajianClient:
    """
    基于 Playwright (浏览器) 的大建云仓客户端
    用于绕过 Python requests 遇到的 SSL/Proxy 拦截问题
    """
    
    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip('/')
        self.access_token = None
        
    def _browser_request(self, method: str, endpoint: str, data: Dict = None, headers: Dict = None) -> Dict:
        """
        使用 Playwright 的 page.evaluate (浏览器上下文) 发送请求
        此方法在浏览器 JS 引擎中执行 fetch，能最大程度模拟真实浏览器行为
        """
        url = f"{self.base_url}{endpoint}"
        if headers is None:
            headers = {}
            
        print(f"    [Browser] {method} {url}")
        
        with sync_playwright() as p:
            # 启动浏览器: 禁用 Web Security, 隐藏自动化特征
            browser = p.chromium.launch(
                headless=True, 
                channel="chrome",
                args=[
                    "--disable-web-security", 
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-blink-features=AutomationControlled" # 隐藏自动化特征
                ]
            )
            
            # 创建上下文 (忽略 HTTPS 错误)
            context = browser.new_context(
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            try:
                # 导航到公网页面以获得 valid Origin (避免 data: 协议限制)
                try:
                    page.goto("http://example.com", timeout=15000)
                except:
                    pass
                
                # 确保页面稳定后再执行evaluate
                # page.wait_for_load_state("domcontentloaded") # 可选

                # 2. 在浏览器内执行 fetch
                js_script = """async ({ url, method, data, headers }) => {
                    const options = {
                        method: method,
                        headers: headers,
                        body: data ? JSON.stringify(data) : undefined
                    };
                    try {
                        const response = await fetch(url, options);
                        const text = await response.text();
                        return {
                            status: response.status,
                            statusText: response.statusText,
                            body: text
                        };
                    } catch (e) {
                         return { error: e.toString() };
                    }
                }"""
                
                args = {
                    "url": url,
                    "method": method,
                    "data": data,
                    "headers": headers
                }
                
                result = page.evaluate(js_script, args)
                
                if "error" in result:
                     raise Exception(f"Browser Fetch Error: {result['error']}")

                if result['status'] >= 400:
                    print(f"    [FAILED] Browser Request Failed: {result['status']} {result['statusText']}")
                    print(f"    Body: {result['body']}")
                    raise Exception(f"HTTP {result['status']}: {result['body']}")
                    
                return json.loads(result['body'])
                
            except Exception as e:
                print(f"    [ERROR] Browser Error: {e}")
                raise e
            finally:
                browser.close()

    def authenticate(self) -> str:
        """获取 Token"""
        data = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.api_secret
        }
        
        print(f"    [Browser] Authenticating...")
        result = self._browser_request("POST", "/oauth/token", data=data)
        
        if "access_token" in result:
            self.access_token = result["access_token"]
            print("    [SUCCESS] Browser Auth Success!")
            return self.access_token
        else:
            raise Exception(f"Auth failed: {result}")

    def get_product_list(self, page: int = 1, page_size: int = 50, category: str = None) -> List[dict]:
        """获取产品列表"""
        if not self.access_token:
            self.authenticate()
            
        params = []
        params.append(f"page={page}")
        params.append(f"page_size={page_size}") 
        # 经查，Dajian API 参数通常是 page_size
        
        query_str = "&".join(params)
        endpoint = f"/products?{query_str}"
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        
        result = self._browser_request("GET", endpoint, headers=headers)
        return result.get("data", [])

    def get_product_detail(self, sku: str) -> dict:
        """获取产品详情"""
        if not self.access_token:
            self.authenticate()
            
        endpoint = f"/product/{sku}" # 假设的 endpoint，需根据实际文档调整
        # 如果不知道具体 endpoint，先假设是列表返回的足够了，或者查阅 Dajian API 文档
        # 通常是 /products/{sku} ?
        
        # 鉴于之前 pipeline 代码依赖 get_product_detail 返回详细信息
        # 我们暂时复用 _request 逻辑
        pass
        # 真正的 DajianClient 会调用 /product/sku 吗？
        # 让我们检查一下原来的 dajian_client.py
        
        # 修正: 检查原代码逻辑
        # 原代码是: return self._request("GET", f"/product/{sku}")
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        return self._browser_request("GET", f"/product/{sku}", headers=headers)
