"""
自动轮转店铺 5% Markdown Sale 促销

策略:
  - 每次促销持续 2 天
  - 上一次促销结束前（或已结束），自动创建下一次
  - 覆盖全店所有已发布产品
  - 确保促销连续不间断

使用 (手动):
  python scripts/auto_rotate_promotions.py                # 检查并创建/续期
  python scripts/auto_rotate_promotions.py --status       # 查看当前促销状态
  python scripts/auto_rotate_promotions.py --force-new    # 强制创建新一期

集成到调度器:
  在 scheduler_daemon.py 中注册 task_promotion_rotate()
  每 6 小时检查一次（足够及时，不会频繁调用 API）
"""
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

DISCOUNT_PCT = 5.0       # 5% off
DURATION_DAYS = 2         # 每期持续 2 天
BUFFER_HOURS = 6          # 结束前 6 小时开始准备下一期
from src.utils.store_profile import get_store_profile

PROMOTION_PREFIX = get_store_profile().promotion_prefix
BEIJING_TZ = timezone(timedelta(hours=8))

def utc_to_beijing(dt_str_or_obj):
    """将 UTC 时间字符串或 datetime 对象转换为北京时间字符串"""
    if isinstance(dt_str_or_obj, str):
        try:
            dt = datetime.fromisoformat(dt_str_or_obj.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return dt_str_or_obj
    else:
        dt = dt_str_or_obj
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ).strftime('%Y-%m-%d %H:%M (北京时间)')


def get_current_promotion_status(service):
    """获取当前运行中和已计划的促销状态"""
    running = service.fetch_promotions('RUNNING')
    scheduled = service.fetch_promotions('SCHEDULED')
    
    our_running = [p for p in running 
                   if p.get('promotionType') in ('MARKDOWN_SALE', 'ITEM_PRICE_MARKDOWN')]
    our_scheduled = [p for p in scheduled
                     if p.get('promotionType') in ('MARKDOWN_SALE', 'ITEM_PRICE_MARKDOWN')]
    
    return our_running, our_scheduled


def needs_new_promotion(running, scheduled):
    """判断是否需要创建新促销"""
    if not running and not scheduled:
        return True, "无活跃促销"
    
    # Check running promotions
    now = datetime.now(timezone.utc)
    for p in running:
        end_str = p.get('endDate', '')
        if end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                remaining = (end_dt - now).total_seconds() / 3600
                if remaining < BUFFER_HOURS:
                    # Check if a scheduled one exists to take over
                    if not scheduled:
                        return True, f"当前促销将在 {remaining:.1f}h 后结束，且无后续促销"
                    else:
                        return False, f"当前促销将在 {remaining:.1f}h 后结束，已有后续促销排队"
                else:
                    return False, f"当前促销还有 {remaining:.1f}h"
            except (ValueError, TypeError):
                pass
    
    # If there are scheduled but no running, wait for them
    if scheduled and not running:
        return False, "有已计划的促销等待生效"
    
    return False, "促销正常运行中"


def create_next_promotion(service, start_after=None):
    """创建下一期促销"""
    now = datetime.now(timezone.utc)
    
    if start_after:
        start = start_after
    else:
        # Start 5 minutes from now
        start = now + timedelta(minutes=5)
    
    end = start + timedelta(days=DURATION_DAYS)
    
    # Generate name with date (北京时间)
    start_bj = start.astimezone(BEIJING_TZ) if start.tzinfo else start.replace(tzinfo=timezone.utc).astimezone(BEIJING_TZ)
    end_bj = end.astimezone(BEIJING_TZ) if end.tzinfo else end.replace(tzinfo=timezone.utc).astimezone(BEIJING_TZ)
    date_str = start_bj.strftime('%m/%d')
    end_date_str = end_bj.strftime('%m/%d')
    name = f"{PROMOTION_PREFIX} {date_str}-{end_date_str}"
    
    logger.info(f"创建新促销: {name}")
    logger.info(f"  折扣: {DISCOUNT_PCT}% off")
    logger.info(f"  时间: {utc_to_beijing(start)} → {utc_to_beijing(end)}")
    
    result = service.create_markdown_sale(
        name=name,
        discount_pct=DISCOUNT_PCT,
        start_date=start,
        end_date=end,
        auto_select_all=True
    )
    
    return result


def show_status(service):
    """显示当前促销状态"""
    running, scheduled = get_current_promotion_status(service)
    
    print("=" * 60)
    print("当前促销状态")
    print("=" * 60)
    
    if running:
        for p in running:
            name = p.get('name', 'N/A')
            end_str = p.get('endDate', '')
            promo_id = p.get('promotionId', 'N/A')
            now = datetime.now(timezone.utc)
            remaining = "N/A"
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                    remaining = f"{(end_dt - now).total_seconds() / 3600:.1f}h"
                except:
                    pass
            print(f"  🟢 运行中: {name}")
            print(f"     ID: {promo_id}")
            print(f"     结束: {utc_to_beijing(end_str)} (剩余 {remaining})")
    else:
        print("  ⚪ 无运行中的促销")
    
    if scheduled:
        for p in scheduled:
            name = p.get('name', 'N/A')
            start_str = p.get('startDate', '')
            print(f"  🔵 已计划: {name} (开始: {utc_to_beijing(start_str)})")
    
    # Local cache
    from src.services.ebay_discount_service import load_discount_state
    state = load_discount_state()
    active = state.get('active_discount')
    if active:
        print(f"\n  📋 本地缓存: {active.get('name', 'N/A')} ({active.get('discount_pct', 0)}% off)")
        print(f"     结束: {utc_to_beijing(active.get('end_date', 'N/A'))}")
    
    print("=" * 60)


def rotate():
    """主函数: 检查并轮转促销"""
    from src.services.ebay_discount_service import EbayDiscountService
    
    service = EbayDiscountService()
    running, scheduled = get_current_promotion_status(service)
    
    need_new, reason = needs_new_promotion(running, scheduled)
    logger.info(f"促销状态: {reason}")
    
    if need_new:
        # Determine start time
        start_after = None
        if running:
            # Schedule after current one ends
            for p in running:
                end_str = p.get('endDate', '')
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                        start_after = end_dt + timedelta(minutes=5)
                    except:
                        pass
        
        result = create_next_promotion(service, start_after)
        if result.get('success'):
            logger.info(f"✅ 新促销已创建: {result.get('name', 'N/A')} (ID: {result.get('promotion_id', 'N/A')})")
            
            # Send notification
            try:
                from src.utils.email_sender import send_email
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
                html = f"""
                <h3>🏷️ 新促销已自动创建</h3>
                <p>时间: {now_str}</p>
                <table style="border-collapse:collapse;width:80%;margin:10px 0;">
                    <tr style="background:#f5f5f5;">
                        <td style="padding:8px;border:1px solid #ddd;">名称</td>
                        <td style="padding:8px;border:1px solid #ddd;font-weight:bold;">{result.get('name', 'N/A')}</td>
                    </tr>
                    <tr>
                        <td style="padding:8px;border:1px solid #ddd;">折扣</td>
                        <td style="padding:8px;border:1px solid #ddd;color:green;">{DISCOUNT_PCT}% off</td>
                    </tr>
                    <tr>
                        <td style="padding:8px;border:1px solid #ddd;">开始</td>
                        <td style="padding:8px;border:1px solid #ddd;">{utc_to_beijing(result.get('start_date', 'N/A'))}</td>
                    </tr>
                    <tr>
                        <td style="padding:8px;border:1px solid #ddd;">结束</td>
                        <td style="padding:8px;border:1px solid #ddd;">{utc_to_beijing(result.get('end_date', 'N/A'))}</td>
                    </tr>
                    <tr>
                        <td style="padding:8px;border:1px solid #ddd;">链接数</td>
                        <td style="padding:8px;border:1px solid #ddd;">{result.get('listing_count', 'N/A')}</td>
                    </tr>
                </table>
                <p style="color:#999;font-size:12px;">此邮件由 auto_rotate_promotions.py 自动发送</p>
                """
                send_email(f"🏷️ 新促销已创建 - {DISCOUNT_PCT}% off {DURATION_DAYS}天", html)
            except Exception as e:
                logger.warning(f"促销通知邮件发送失败: {e}")
        else:
            logger.error(f"❌ 创建促销失败: {result.get('error', 'Unknown error')}")
    else:
        logger.info("✅ 促销正常，无需轮转")
    
    return need_new


def main():
    import argparse
    parser = argparse.ArgumentParser(description='自动轮转 5% 店铺促销')
    parser.add_argument('--status', action='store_true', help='查看当前促销状态')
    parser.add_argument('--force-new', action='store_true', help='强制创建新一期促销')
    parser.add_argument('--email', action='store_true', help='发送结果邮件')
    args = parser.parse_args()
    
    from src.services.ebay_discount_service import EbayDiscountService
    service = EbayDiscountService()
    
    if args.status:
        show_status(service)
        return
    
    if args.force_new:
        result = create_next_promotion(service)
        if result.get('success'):
            print(f"✅ 新促销已创建: {result.get('name')} (ID: {result.get('promotion_id')})")
        else:
            print(f"❌ 创建失败: {result.get('error')}")
        return
    
    # Normal rotation check
    rotate()


if __name__ == "__main__":
    main()
