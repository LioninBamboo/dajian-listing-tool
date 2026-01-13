#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
使用政策 ID 发布 eBay 产品脚本
"""

import argparse
import sys
import requests
from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager

def publish_product(sku, title, price, quantity, 
                   fulfillment_policy_id, return_policy_id, payment_policy_id):
    """发布产品到 eBay"""
    
    try:
        # 初始化服务
        print("[*] 初始化 OAuth 服务...")
        oauth_service = EbayOAuthService()
        print("[OK] OAuth 服务已初始化")
        
        print("[*] 初始化政策管理器...")
        policy_manager = EbayPolicyManager(oauth_service)
        print("[OK] 政策管理器已初始化")
        
        # 初始化客户端
        print("[*] 初始化 eBay API 客户端...")
        client = RealEbayClient(oauth_service, policy_manager)
        print("[OK] 客户端已初始化")
        
        # 检查授权
        print("[*] 检查授权...")
        if not client.is_authorized():
            print("[ERROR] 未授权")
            return False
        print("[OK] 已授权")
        
        # 创建或更新库存项目
        print(f"\n[*] 创建库存项目: {sku}")
        inventory_result = client.create_or_replace_inventory_item(
            sku=sku,
            title=title,
            price=float(price),
            quantity=int(quantity)
        )
        
        if inventory_result.get('status') != 'success':
            print(f"[ERROR] 库存创建失败: {inventory_result.get('message')}")
            return False
        
        print(f"[OK] 库存项目已创建/更新")
        
        # 创建 Offer
        print(f"\n[*] 创建 Offer...")
        offer_result = client.create_offer(
            sku=sku,
            marketplace_id='EBAY_US',
            fulfillment_policy_id=fulfillment_policy_id,
            return_policy_id=return_policy_id,
            payment_policy_id=payment_policy_id
        )
        
        if offer_result.get('status') != 'success':
            print(f"[ERROR] Offer 创建失败: {offer_result.get('message')}")
            return False
        
        offer_id = offer_result.get('offer_id')
        print(f"[OK] Offer 已创建: {offer_id}")
        
        # 发布 Offer
        print(f"\n[*] 发布 Offer...")
        publish_result = client.publish_offer(offer_id)
        
        if publish_result.get('status') != 'success':
            print(f"[ERROR] 发布失败: {publish_result.get('message')}")
            return False
        
        print(f"[OK] Offer 已发布!")
        
        # 显示成功信息
        print("\n" + "=" * 60)
        print("✅ 产品发布成功！")
        print("=" * 60)
        print(f"SKU: {sku}")
        print(f"标题: {title}")
        print(f"价格: ${price}")
        print(f"数量: {quantity}")
        print(f"Offer ID: {offer_id}")
        print("\n产品现已在 eBay 上架！")
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print(f"[ERROR] 异常: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def main():
    parser = argparse.ArgumentParser(description='发布产品到 eBay')
    
    # 产品信息参数
    parser.add_argument('--sku', help='产品 SKU')
    parser.add_argument('--title', help='产品标题')
    parser.add_argument('--price', type=float, help='产品价格')
    parser.add_argument('--qty', type=int, default=1, help='产品数量 (默认: 1)')
    
    # 政策 ID 参数
    parser.add_argument('--fulfillment_policy_id', help='运输政策 ID')
    parser.add_argument('--return_policy_id', help='退货政策 ID')
    parser.add_argument('--payment_policy_id', help='付款政策 ID')
    
    # 演示模式
    parser.add_argument('--demo', action='store_true', help='使用演示数据')
    
    args = parser.parse_args()
    
    if args.demo:
        # 演示模式：使用预设的政策 ID
        print("\n" + "=" * 60)
        print("演示模式 - 使用预设数据")
        print("=" * 60)
        
        sku = "DEMO-PRODUCT-001"
        title = "演示产品 - 测试发布"
        price = 9.99
        quantity = 5
        
        # 使用您获取的真实政策 ID
        fulfillment_policy_id = "321875060021"  # GIGA Shipping
        return_policy_id = "321875028021"        # GIGA 30days Return
        payment_policy_id = "321874983021"       # Combine
        
        print(f"SKU: {sku}")
        print(f"标题: {title}")
        print(f"价格: ${price}")
        print(f"数量: {quantity}")
        print(f"运输政策 ID: {fulfillment_policy_id}")
        print(f"退货政策 ID: {return_policy_id}")
        print(f"付款政策 ID: {payment_policy_id}")
        print("=" * 60 + "\n")
        
        success = publish_product(
            sku, title, price, quantity,
            fulfillment_policy_id, return_policy_id, payment_policy_id
        )
    else:
        # 检查必需参数
        if not all([args.sku, args.title, args.price, 
                   args.fulfillment_policy_id, args.return_policy_id, args.payment_policy_id]):
            parser.error("--demo 模式或必须提供所有参数")
        
        success = publish_product(
            args.sku, args.title, args.price, args.qty,
            args.fulfillment_policy_id, args.return_policy_id, args.payment_policy_id
        )
    
    return 0 if success else 1

if __name__ == '__main__':
    sys.exit(main())
