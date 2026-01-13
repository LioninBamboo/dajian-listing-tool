from typing import Any, List
import time

class MockDaJianClient:
    """
    模拟大建云仓客户端
    用于在网络不通的情况下测试后续流程 (Gemini + eBay)
    """
    
    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = ""):
        self.api_key = "mock_key"
        self.api_secret = "mock_secret"
        
    def authenticate(self) -> str:
        print("    [Mock] Authenticating... Success!")
        return "mock_access_token_12345"
        
    def get_product_list(
        self,
        page: int = 1,
        page_size: int = 50,
        category: str | None = None
    ) -> List[dict[str, Any]]:
        print(f"    [Mock] Fetching product list (Page {page})...")
        
        # Only return data for the first page
        if page > 1:
            return []
            
        # 返回一个测试产品
        return [{
            "sku": "MOCK-TEST-001",
            "title": "Modern Ergonomic Office Chair Mesh Back Swivel Desk Chair",
            "stock": 50,
            "price": 89.99,
            "category": "Office Furniture"
        }]
    
    def get_product_detail(self, sku: str) -> dict[str, Any]:
        print(f"    [Mock] Fetching product detail for {sku}...")
        
        return {
            "sku": sku,
            "title": "Modern Ergonomic Office Chair Mesh Back Swivel Desk Chair",
            "description": """
                <html>
                <body>
                    <h2>Product Description</h2>
                    <p>This ergonomic office chair is designed for comfort and productivity. It features a breathable mesh back, adjustable height, and smooth-rolling casters.</p>
                    <ul>
                        <li>Breathable mesh back support</li>
                        <li>Adjustable seat height</li>
                        <li>360-degree swivel</li>
                        <li>Heavy-duty base</li>
                    </ul>
                    <p>Perfect for home office or corporate settings.</p>
                </body>
                </html>
            """,
            "images": [
                "https://i.ebayimg.com/images/g/FjoAAOSwnbZlq~0L/s-l1600.jpg", # 使用一个真实的eBay图片URL测试
                "https://i.ebayimg.com/images/g/FjoAAOSwnbZlq~0L/s-l500.jpg"
            ],
            "stock": 50,
            "price": 89.99,
            "weight": 25.5,
            "dimensions": {"length": 24, "width": 24, "height": 40},
            "category": "Office Furniture"
        }
