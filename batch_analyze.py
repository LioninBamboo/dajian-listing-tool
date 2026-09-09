#!/usr/bin/env python3
"""批量分析所有 COLLECTED 状态的产品"""
import os
import sys
sys.path.insert(0, '.')
from datetime import UTC, datetime

from dotenv import load_dotenv
load_dotenv()

from src.db.collection_db import SessionLocal
from src.db.collection_models import CollectedProduct
from src.services.ebay_category_matcher import create_category_matcher
from src.services.pricing_engine import PricingEngine
from sqlalchemy.orm.attributes import flag_modified
from qwen_optimizer import QwenOptimizer, optimize_product_full_with_timeout
from src.utils.listing_quality_gate import (
    blocking_issue_messages,
    normalize_generated_listing,
    validate_listing_quality,
)

# 初始化
db = SessionLocal()
QWEN_KEY = os.getenv("QWEN_API_KEY")
CATEGORY_MATCHER = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

def main():
    if not QWEN_KEY:
        print("❌ QWEN_API_KEY 未设置")
        sys.exit(1)

    qwen = QwenOptimizer(api_key=QWEN_KEY)

    # 获取所有 COLLECTED 状态的产品
    products = db.query(CollectedProduct).filter_by(status='COLLECTED').all()

    if not products:
        print("✅ 没有需要处理的 COLLECTED 状态产品")
        sys.exit(0)

    print(f"📦 发现 {len(products)} 个待处理产品\n")

    success = 0
    failed = 0

    for i, product in enumerate(products, 1):
        print(f"[{i}/{len(products)}] 处理 {product.sku}...")
        
        try:
            # 1. 计算价格
            specs = product.specs or {}
            attributes = product.attributes or {}
            is_oversize = 'Dimensions' in specs or 'Dimensions' in attributes
            
            dajian_costs = PricingEngine.calculate_dajian_cost(
                product_price=product.price,
                shipping_cost=product.shipping,
                is_oversize=is_oversize
            )
            
            safe_price = PricingEngine.calculate_selling_price(dajian_costs["total_dajian_cost"], 0.15)
            
            # 2. AI 优化
            opt_data = None
            previous_errors = []
            for attempt in range(3):
                try:
                    opt_data = optimize_product_full_with_timeout(
                        api_key=QWEN_KEY,
                        original_title=product.title,
                        original_description=product.description or '',
                        attributes=attributes,
                        specs=specs,
                        images=product.images or [],
                        previous_errors=previous_errors if previous_errors else None
                    )
                    opt_data = normalize_generated_listing(
                        opt_data,
                        source_title=product.title,
                        source_description=product.description or '',
                        attributes=attributes,
                        specs=specs,
                        images=product.images or [],
                        videos=product.videos or [],
                        category_matcher=CATEGORY_MATCHER,
                    )
                    quality_issues = validate_listing_quality(
                        opt_data,
                        source_title=product.title,
                        source_description=product.description or '',
                        attributes=attributes,
                        specs=specs,
                        images=product.images or [],
                        videos=product.videos or [],
                        category_matcher=CATEGORY_MATCHER,
                    )
                    blockers = blocking_issue_messages(quality_issues)
                    if blockers:
                        raise ValueError("Listing quality gate failed: " + "; ".join(blockers[:8]))
                    break # Success!
                except ValueError as ve:
                    if attempt < 2 and "quality gate failed" in str(ve):
                        print(f"  [Attempt {attempt+1}/3] ❌ {ve}. Retrying...")
                        err_str = str(ve).replace("Listing quality gate failed: ", "")
                        previous_errors = err_str.split("; ")
                    else:
                        raise # Give up after 3 tries or if it's not a quality gate issue
            
            # 3. 保存
            product.optimization = opt_data
            product.cost_breakdown = dajian_costs
            product.suggested_price = safe_price['selling_price']
            product.status = "READY"
            
            flag_modified(product, 'optimization')
            flag_modified(product, 'cost_breakdown')
            
            db.commit()
            print(f"  ✅ 完成\n")
            success += 1
            
        except Exception as e:
            print(f"  ❌ 失败: {e}\n")
            failed += 1
            db.rollback()
            product = db.merge(product)
            product.status = "ERROR"
            product.logs = (product.logs or []) + [
                f"Batch analysis error at {_utcnow_naive().isoformat()}: {e}"
            ]
            flag_modified(product, 'logs')
            db.commit()

    db.close()

    print(f"\n{'='*50}")
    print(f"📊 处理完成: 成功 {success}, 失败 {failed}")

if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    main()
