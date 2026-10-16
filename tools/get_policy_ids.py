#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
获取 eBay 政策 ID 脚本
从 eBay API 获取您创建的所有政策的 ID
"""

import os
import sys
import requests
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

def get_policy_ids():
    """从 eBay API 获取所有政策 ID"""
    
    # 获取认证 token
    try:
        from src.services.ebay_auth import EbayOAuthService
        auth_service = EbayOAuthService()
        token = auth_service.get_valid_token()
        
        if not token:
            print("[ERROR] 无法获取认证 token")
            return False
        
        print(f"[OK] 已获取认证 token")
        
    except Exception as e:
        print(f"[ERROR] 认证失败: {str(e)}")
        return False
    
    # 获取运输政策
    print("\n==== 获取运输政策 ====")
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.get(
            'https://api.ebay.com/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US',
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            policies = data.get('fulfillmentPolicies', [])
            
            if policies:
                print(f"[OK] 找到 {len(policies)} 个运输政策:")
                for policy in policies:
                    policy_id = policy.get('fulfillmentPolicyId')
                    name = policy.get('name')
                    print(f"  - ID: {policy_id}, 名称: {name}")
            else:
                print("[!] 未找到运输政策")
        else:
            print(f"[ERROR] 获取运输政策失败: {response.status_code}")
            print(f"Response: {response.text}")
    
    except Exception as e:
        print(f"[ERROR] 获取运输政策异常: {str(e)}")
    
    # 获取退货政策
    print("\n==== 获取退货政策 ====")
    try:
        response = requests.get(
            'https://api.ebay.com/sell/account/v1/return_policy?marketplace_id=EBAY_US',
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            policies = data.get('returnPolicies', [])
            
            if policies:
                print(f"[OK] 找到 {len(policies)} 个退货政策:")
                for policy in policies:
                    policy_id = policy.get('returnPolicyId')
                    name = policy.get('name')
                    print(f"  - ID: {policy_id}, 名称: {name}")
            else:
                print("[!] 未找到退货政策")
        else:
            print(f"[ERROR] 获取退货政策失败: {response.status_code}")
            print(f"Response: {response.text}")
    
    except Exception as e:
        print(f"[ERROR] 获取退货政策异常: {str(e)}")
    
    # 获取付款政策
    print("\n==== 获取付款政策 ====")
    try:
        response = requests.get(
            'https://api.ebay.com/sell/account/v1/payment_policy?marketplace_id=EBAY_US',
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            policies = data.get('paymentPolicies', [])
            
            if policies:
                print(f"[OK] 找到 {len(policies)} 个付款政策:")
                for policy in policies:
                    policy_id = policy.get('paymentPolicyId')
                    name = policy.get('name')
                    print(f"  - ID: {policy_id}, 名称: {name}")
            else:
                print("[!] 未找到付款政策")
        else:
            print(f"[ERROR] 获取付款政策失败: {response.status_code}")
            print(f"Response: {response.text}")
    
    except Exception as e:
        print(f"[ERROR] 获取付款政策异常: {str(e)}")
    
    return True

if __name__ == '__main__':
    print("=" * 60)
    print("eBay 政策 ID 获取工具")
    print("=" * 60)
    print("\n正在从 eBay API 获取您的政策 ID...\n")
    
    success = get_policy_ids()
    
    if success:
        print("\n" + "=" * 60)
        print("使用这些政策 ID 发布产品：")
        print("  python publish_with_policies.py \\")
        print("    --sku YOUR-SKU \\")
        print("    --title 'Product Title' \\")
        print("    --price 19.99 \\")
        print("    --fulfillment_policy_id <政策ID> \\")
        print("    --return_policy_id <政策ID> \\")
        print("    --payment_policy_id <政策ID>")
        print("=" * 60)
