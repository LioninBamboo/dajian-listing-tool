"""
测试 Qwen AI 优化功能
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

def main():
    print("=" * 60)
    print("Qwen AI 优化功能测试")
    print("=" * 60)
    
    qwen_key = os.getenv('QWEN_API_KEY')
    print(f"QWEN_API_KEY: {qwen_key[:20]}..." if qwen_key else "未设置")
    
    if not qwen_key:
        print("✗ QWEN_API_KEY 未设置")
        return
    
    # 导入 Qwen 优化器
    from qwen_optimizer import QwenOptimizer
    
    optimizer = QwenOptimizer()
    print(f"✓ Qwen Optimizer 初始化成功")
    
    # 测试产品数据
    test_product = {
        "sku": "TEST-001",
        "title": "Modern Green Velvet Sofa 3 Seater Living Room Furniture",
        "description": "This is a beautiful green velvet sofa with wooden legs. Perfect for modern living rooms.",
        "price": 599.99,
        "category": "Sofas",
        "images": ["https://example.com/sofa1.jpg"],
        "specifications": {
            "Color": "Green",
            "Material": "Velvet",
            "Dimensions": "80 x 35 x 32 inches",
            "Weight": "120 lbs"
        }
    }
    
    print("\n--- 测试标题优化 ---")
    print(f"原始标题: {test_product['title']}")
    
    try:
        result = optimizer.optimize_title(
            test_product['title'], 
            category=test_product.get('category', ''),
            description=test_product.get('description', '')
        )
        if result:
            print(f"✓ 优化后标题: {result}")
        else:
            print("✗ 标题优化失败")
    except Exception as e:
        print(f"✗ 标题优化错误: {e}")
    
    print("\n--- 测试完整产品优化 ---")
    try:
        result = optimizer.optimize_product_full(
            original_title=test_product['title'],
            original_description=test_product['description'],
            attributes=test_product.get('specifications', {}),
            specs=test_product.get('specifications', {})
        )
        if result:
            print(f"✓ 优化结果包含: {list(result.keys()) if isinstance(result, dict) else type(result)}")
            if isinstance(result, dict):
                if 'title' in result:
                    print(f"  标题: {result['title'][:80]}...")
                if 'description' in result:
                    print(f"  描述前200字符: {result['description'][:200]}...")
        else:
            print("✗ 完整优化失败")
    except Exception as e:
        print(f"✗ 完整优化错误: {e}")
    
    print("\n--- 测试尺寸提取 ---")
    try:
        dims = optimizer.extract_dimensions(
            attributes=test_product.get('specifications', {}),
            specs=test_product.get('specifications', {}),
            description=test_product.get('description', '')
        )
        print(f"✓ 提取的尺寸: {dims}")
    except Exception as e:
        print(f"✗ 尺寸提取错误: {e}")
    
    print("\n" + "=" * 60)
    print("Qwen AI 测试完成")
    print("=" * 60)

if __name__ == "__main__":
    main()
