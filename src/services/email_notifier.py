"""
邮件通知服务
用于发送 eBay 优化任务完成通知
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os


class EmailNotifier:
    """邮件通知器"""
    
    def __init__(self, smtp_server="smtp.gmail.com", smtp_port=587):
        """
        初始化邮件通知器
        
        Args:
            smtp_server: SMTP 服务器地址
            smtp_port: SMTP 端口
        """
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        
    def send_optimization_report(
        self,
        to_email: str,
        total_processed: int,
        total_updated: int,
        failed_count: int = 0,
        report_file: str = None
    ):
        """
        发送优化报告邮件
        
        Args:
            to_email: 收件人邮箱
            total_processed: 处理总数
            total_updated: 更新成功数
            failed_count: 失败数
            report_file: 报告文件路径
        """
        # 从环境变量获取发件邮箱和密码
        from_email = os.getenv("NOTIFICATION_EMAIL")
        password = os.getenv("NOTIFICATION_EMAIL_PASSWORD")
        
        if not from_email or not password:
            print("⚠️ 未配置邮件通知凭据 (NOTIFICATION_EMAIL, NOTIFICATION_EMAIL_PASSWORD)")
            return False
        
        # 构建邮件内容
        subject = f"eBay Listing 优化完成 - {datetime.now().strftime('%Y-%m-%d')}"
        
        # HTML 邮件正文
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2 style="color: #0066cc;">eBay Listing 优化报告</h2>
            <p>优化任务已完成，详情如下：</p>
            
            <table style="border-collapse: collapse; width: 100%; max-width: 500px;">
                <tr style="background-color: #f2f2f2;">
                    <td style="padding: 10px; border: 1px solid #ddd;"><strong>执行时间</strong></td>
                    <td style="padding: 10px; border: 1px solid #ddd;">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; border: 1px solid #ddd;"><strong>处理总数</strong></td>
                    <td style="padding: 10px; border: 1px solid #ddd;">{total_processed}</td>
                </tr>
                <tr style="background-color: #f2f2f2;">
                    <td style="padding: 10px; border: 1px solid #ddd;"><strong>更新成功</strong></td>
                    <td style="padding: 10px; border: 1px solid #ddd; color: green;"><strong>{total_updated}</strong></td>
                </tr>
                <tr>
                    <td style="padding: 10px; border: 1px solid #ddd;"><strong>失败数量</strong></td>
                    <td style="padding: 10px; border: 1px solid #ddd; color: {'red' if failed_count > 0 else 'green'};">{failed_count}</td>
                </tr>
            </table>
            
            <p style="margin-top: 20px;">
                {'<strong style="color: green;">✅ 所有优化均已成功同步到 eBay</strong>' if failed_count == 0 else '<strong style="color: orange;">⚠️ 部分优化失败，请检查日志</strong>'}
            </p>
            
            {f'<p>详细报告: <a href="file:///{report_file}">{os.path.basename(report_file)}</a></p>' if report_file else ''}
            
            <hr style="margin-top: 30px;">
            <p style="color: #666; font-size: 12px;">
                此邮件由 eBay 自动化优化系统发送<br>
                如有问题，请检查 daily_optimize.log
            </p>
        </body>
        </html>
        """
        
        # 创建邮件
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = from_email
        msg['To'] = to_email
        
        # 添加 HTML 内容
        html_part = MIMEText(html_content, 'html', 'utf-8')
        msg.attach(html_part)
        
        # 发送邮件
        try:
            print(f"📧 正在发送邮件到 {to_email}...")
            
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(from_email, password)
                server.send_message(msg)
            
            print(f"✅ 邮件发送成功！")
            return True
            
        except Exception as e:
            print(f"❌ 邮件发送失败: {e}")
            return False
