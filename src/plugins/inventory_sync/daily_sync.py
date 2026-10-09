"""
独立库存同步脚本 (仅供手动调试/测试使用)

⚠️ 日常库存同步已合并到 daily_tasks.py (scheduler 09:30 任务)。
   daily_tasks.py 的 sync_inventory() 调用同一个 InventorySyncService.sync_all()
   并将结果整合到每日汇总邮件中。

   本脚本保留为独立调试工具，不再由调度器自动调用。

推荐入口:
    python daily_tasks.py --sync-only

调试用法:
    python src/plugins/inventory_sync/daily_sync.py [--dry-run] [--email]
    
参数:
    --dry-run   测试模式，不实际更新 eBay
    --email     发送邮件通知 (独立调试报告，不是主流程默认邮件)
"""
import sys
import os
import argparse
import logging
from pathlib import Path
from datetime import datetime
from collections import Counter

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# 确保工作目录为项目根目录（计划任务可能在其他目录启动）
os.chdir(PROJECT_ROOT)

# 设置日志
log_dir = PROJECT_ROOT / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / f"inventory_sync_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_notification_recipient() -> str:
    """Return the configured notification mailbox for manual inventory sync reports."""
    return os.getenv("NOTIFICATION_EMAIL", "").strip()


def maybe_send_email_report(results: list) -> bool:
    """Send the manual inventory sync email only when a notification address is configured."""
    to_email = get_notification_recipient()
    if not to_email:
        logger.warning("未配置 NOTIFICATION_EMAIL，跳过库存同步邮件发送")
        return False
    return bool(send_email_report(results, to_email))


def send_email_report(results: list, to_email: str):
    """发送同步报告邮件"""
    from src.services.email_notifier import EmailNotifier
    
    # 统计结果
    action_counts = Counter(r.action for r in results)
    
    # 构建报告内容
    summary_lines = [
        f"📦 库存同步报告 - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"总处理: {len(results)} 个产品",
        f"✅ 无变化: {action_counts.get('no_change', 0)}",
        f"🔴 库存归零: {action_counts.get('out_of_stock', 0)}",
        f"� 补货上架: {action_counts.get('restocked', 0)}",
        f"�💰 价格更新: {action_counts.get('price_updated', 0)}",
        f"⏭️ 已跳过: {action_counts.get('skipped', 0)}",
        f"❌ 错误: {action_counts.get('error', 0)}",
        "",
    ]
    
    # 添加详细变更记录
    changes = [r for r in results if r.action in ('out_of_stock', 'price_updated', 'restocked')]
    if changes:
        summary_lines.append("=== 变更详情 ===")
        for r in changes[:20]:  # 最多显示 20 条
            if r.action == 'out_of_stock':
                summary_lines.append(f"🔴 {r.sku}: {r.message}")
            elif r.action == 'restocked':
                summary_lines.append(f"🟢 {r.sku}: {r.message}")
            elif r.action == 'price_updated':
                summary_lines.append(f"💰 {r.sku}: {r.old_value} → {r.new_value}")
    
    # 添加错误记录
    errors = [r for r in results if r.action == 'error']
    if errors:
        summary_lines.append("")
        summary_lines.append("=== 错误详情 ===")
        for r in errors[:10]:  # 最多显示 10 条
            summary_lines.append(f"❌ {r.sku}: {r.message}")
    
    report_text = "\n".join(summary_lines)
    
    # 尝试发送邮件
    notifier = EmailNotifier()
    
    from_email = os.getenv("NOTIFICATION_EMAIL")
    password = os.getenv("NOTIFICATION_EMAIL_PASSWORD")
    
    if not from_email or not password:
        logger.warning("未配置邮件凭据，跳过邮件发送")
        logger.info("报告内容:\n" + report_text)
        return False
    
    try:
        # 使用通用邮件发送（需要扩展 EmailNotifier）
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"📦 大建库存同步报告 - {datetime.now().strftime('%Y-%m-%d')}"
        msg['From'] = from_email
        msg['To'] = to_email
        
        # 纯文本版本
        text_part = MIMEText(report_text, 'plain', 'utf-8')
        msg.attach(text_part)
        
        # HTML 版本
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h2 style="color: #0066cc;">📦 大建库存同步报告</h2>
            <p>执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            
            <table style="border-collapse: collapse; margin: 20px 0;">
                <tr style="background: #f5f5f5;">
                    <td style="padding: 10px; border: 1px solid #ddd;"><strong>总处理</strong></td>
                    <td style="padding: 10px; border: 1px solid #ddd;">{len(results)}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; border: 1px solid #ddd;">✅ 无变化</td>
                    <td style="padding: 10px; border: 1px solid #ddd;">{action_counts.get('no_change', 0)}</td>
                </tr>
                <tr style="background: #ffebee;">
                    <td style="padding: 10px; border: 1px solid #ddd;">🔴 库存归零</td>
                    <td style="padding: 10px; border: 1px solid #ddd; color: red;"><strong>{action_counts.get('out_of_stock', 0)}</strong></td>
                </tr>
                <tr style="background: #e8f5e9;">
                    <td style="padding: 10px; border: 1px solid #ddd;">🟢 补货上架</td>
                    <td style="padding: 10px; border: 1px solid #ddd; color: green;"><strong>{action_counts.get('restocked', 0)}</strong></td>
                </tr>
                <tr style="background: #fff3e0;">
                    <td style="padding: 10px; border: 1px solid #ddd;">💰 价格更新</td>
                    <td style="padding: 10px; border: 1px solid #ddd; color: orange;"><strong>{action_counts.get('price_updated', 0)}</strong></td>
                </tr>
                <tr>
                    <td style="padding: 10px; border: 1px solid #ddd;">⏭️ 已跳过</td>
                    <td style="padding: 10px; border: 1px solid #ddd;">{action_counts.get('skipped', 0)}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; border: 1px solid #ddd;">❌ 错误</td>
                    <td style="padding: 10px; border: 1px solid #ddd; color: red;">{action_counts.get('error', 0)}</td>
                </tr>
            </table>
        """
        
        if changes:
            html_content += "<h3>变更详情</h3><ul>"
            for r in changes[:20]:
                if r.action == 'out_of_stock':
                    html_content += f"<li>🔴 <strong>{r.sku}</strong>: {r.message}</li>"
                elif r.action == 'restocked':
                    html_content += f"<li>🟢 <strong>{r.sku}</strong>: {r.message}</li>"
                elif r.action == 'price_updated':
                    html_content += f"<li>💰 <strong>{r.sku}</strong>: {r.old_value} → {r.new_value}</li>"
            html_content += "</ul>"
        
        html_content += "</body></html>"
        
        html_part = MIMEText(html_content, 'html', 'utf-8')
        msg.attach(html_part)
        
        # 使用统一邮件模块发送
        from src.utils.email_sender import send_email
        result = send_email(
            subject=msg['Subject'],
            html_body=html_content,
            to_email=to_email
        )
        if result:
            logger.info(f"✅ 邮件已发送到 {to_email}")
        else:
            logger.warning("邮件发送失败，报告已保存到本地")
            logger.info("报告内容:\n" + report_text)
        return result
        
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        logger.info("报告内容:\n" + report_text)
        return False


def main():
    parser = argparse.ArgumentParser(description='每日大建库存同步')
    parser.add_argument('--dry-run', action='store_true', help='测试模式，不实际更新')
    parser.add_argument('--email', action='store_true', help='发送邮件通知')
    parser.add_argument('--limit', type=int, default=0, help='限制处理数量（调试用）')
    args = parser.parse_args()
    
    logger.info("=" * 50)
    logger.info("开始每日库存同步")
    logger.info(f"模式: {'测试模式' if args.dry_run else '正式执行'}")
    logger.info("=" * 50)
    
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    
    service = InventorySyncService()
    results = service.sync_all(dry_run=args.dry_run)
    
    if args.limit:
        results = results[:args.limit]
    
    # 统计
    action_counts = Counter(r.action for r in results)
    
    logger.info("=" * 50)
    logger.info("同步完成!")
    logger.info(f"总处理: {len(results)}")
    logger.info(f"无变化: {action_counts.get('no_change', 0)}")
    logger.info(f"库存归零: {action_counts.get('out_of_stock', 0)}")
    logger.info(f"补货上架: {action_counts.get('restocked', 0)}")
    logger.info(f"价格更新: {action_counts.get('price_updated', 0)}")
    logger.info(f"跳过: {action_counts.get('skipped', 0)}")
    logger.info(f"错误: {action_counts.get('error', 0)}")
    logger.info("=" * 50)
    
    # 发送邮件
    if args.email:
        maybe_send_email_report(results)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"脚本异常退出: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        logging.shutdown()
