"""
统一邮件发送模块 — 支持多 SMTP 提供商自动切换 + 本地报告备份

支持的提供商（按优先级）：
1. 163 邮箱  (smtp.163.com:465 SSL)
2. Gmail     (smtp.gmail.com:465 SSL / 587 STARTTLS)
3. Outlook   (smtp-mail.outlook.com:587 STARTTLS)
4. QQ 邮箱   (smtp.qq.com:465 SSL)

配置方式（.env）：
    NOTIFICATION_EMAIL=your@163.com              # 发件人邮箱
    NOTIFICATION_EMAIL_PASSWORD=your_auth_code    # 授权码（非登录密码）
    SMTP_PROVIDER=auto                            # auto | 163 | gmail | outlook | qq

当 SMTP_PROVIDER=auto 时，自动根据邮箱后缀检测提供商。
若所有 SMTP 方式均失败，报告仍会保存在 reports/ 目录。
"""

import os
import logging
import mimetypes
import re
import smtplib
import socket
import ssl
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from pathlib import Path
from typing import Optional, List, Tuple

import requests
from dotenv import load_dotenv
from src.utils.report_retention import EMAIL_REPORT_PATTERNS, purge_named_artifacts

logger = logging.getLogger(__name__)
IMG_SRC_PATTERN = re.compile(r'(<img\b[^>]*?\bsrc=["\'])(https?://[^"\']+)(["\'][^>]*>)', re.IGNORECASE)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_LOADED = False
EMAIL_REPORT_RETENTION_DAYS = int(os.getenv("EMAIL_REPORT_RETENTION_DAYS", "3") or 3)
# ---------------------------------------------------------------------------
# SMTP 提供商配置: (显示名, 主机, 端口, 是否SSL, 是否STARTTLS)
# ---------------------------------------------------------------------------
SMTP_PROVIDERS = {
    "163": [
        ("163-SSL", "smtp.163.com", 465, True, False),
    ],
    "gmail": [
        ("Gmail-SSL", "smtp.gmail.com", 465, True, False),
        ("Gmail-STARTTLS", "smtp.gmail.com", 587, False, True),
    ],
    "outlook": [
        ("Outlook-STARTTLS", "smtp-mail.outlook.com", 587, False, True),
    ],
    "hotmail": [
        ("Hotmail-STARTTLS", "smtp-mail.outlook.com", 587, False, True),
    ],
    "qq": [
        ("QQ-SSL", "smtp.qq.com", 465, True, False),
    ],
}


# ---------------------------------------------------------------------------
# 提供商检测 & 连接方式排序
# ---------------------------------------------------------------------------
def detect_provider(email: str) -> str:
    """根据邮箱后缀检测 SMTP 提供商"""
    email = email.lower()
    if "@163." in email:
        return "163"
    elif "@gmail." in email:
        return "gmail"
    elif "@outlook." in email or "@hotmail." in email or "@live." in email:
        return "outlook"
    elif "@qq." in email:
        return "qq"
    return "163"  # 默认用 163


def _smtp_local_hostname() -> str:
    """Return an EHLO/HELO name 163 and other picky SMTPs will accept.

    Windows DHCP/NetBIOS names can become ``LioninBamboo.DHCP HOST`` (space
    included). 163 replies ``500 Error: bad syntax`` to that EHLO and then
    every scheduled email silently fails.
    """
    raw = (socket.getfqdn() or socket.gethostname() or "").strip()
    if (
        not raw
        or " " in raw
        or any(ord(ch) > 127 for ch in raw)
        or raw.startswith(".")
        or raw.endswith(".")
    ):
        return "localhost"
    return raw


def get_smtp_attempts(email: str) -> List[Tuple[str, str, int, bool, bool]]:
    """
    获取所有要尝试的 SMTP 连接方式（按优先级排列）。
    优先匹配邮箱的提供商，再追加其他方式作为后备。
    """
    forced = os.getenv("SMTP_PROVIDER", "auto").lower()
    if forced != "auto" and forced in SMTP_PROVIDERS:
        return list(SMTP_PROVIDERS[forced])

    primary = detect_provider(email)
    attempts = list(SMTP_PROVIDERS.get(primary, []))

    # 追加其他提供商（去重）
    seen_hosts = {(a[1], a[2]) for a in attempts}
    for provider_key, configs in SMTP_PROVIDERS.items():
        if provider_key == primary:
            continue
        for cfg in configs:
            if (cfg[1], cfg[2]) not in seen_hosts:
                attempts.append(cfg)
                seen_hosts.add((cfg[1], cfg[2]))

    return attempts


# ---------------------------------------------------------------------------
# 主发送函数
# ---------------------------------------------------------------------------
def send_email(
    subject: str,
    html_body: str,
    to_email: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    retry_delay: float = 2.0,
) -> bool:
    """
    发送 HTML 邮件（自动多提供商重试）。

    无论 SMTP 是否成功，都会先保存一份本地 HTML 报告到 reports/。

    Args:
        subject:      邮件主题
        html_body:    HTML 格式正文
        to_email:     收件人（默认发给自己）
        attachments:  附件文件路径列表
        retry_delay:  两次尝试之间的间隔秒数

    Returns:
        是否发送成功（True=邮件已送达, False=所有方式均失败但报告已保存本地）
    """
    _ensure_env_loaded()

    # ---- 本地保存（保底） ----
    local_path = _save_local_report(subject, html_body)

    # ---- 检查凭据 ----
    sender = os.getenv("NOTIFICATION_EMAIL", "").strip().strip("'\"")
    password = os.getenv("NOTIFICATION_EMAIL_PASSWORD", "").strip().strip("'\"")

    if not sender or not password:
        logger.warning("邮件凭据未配置 (NOTIFICATION_EMAIL / NOTIFICATION_EMAIL_PASSWORD)")
        logger.info(f"📄 报告已保存到本地: {local_path}")
        _send_toast_notification(subject, local_path)
        return False

    if not to_email:
        to_email = sender

    # ---- 构建 MIME 邮件 ----
    msg = _build_mime_message(sender, to_email, subject, html_body, attachments)

    # ---- 依次尝试所有 SMTP 方式 ----
    attempts = get_smtp_attempts(sender)

    for method_name, host, port, use_ssl, use_starttls in attempts:
        try:
            _send_via_smtp(host, port, use_ssl, use_starttls,
                           sender, password, to_email, msg)
            logger.info(f"📧 邮件已发送: {subject[:50]}... (via {method_name})")
            return True
        except Exception as e:
            logger.warning(f"SMTP {method_name} ({host}:{port}) 失败: {e}")
            time.sleep(retry_delay)

    # 全部失败
    logger.error(f"邮件发送失败（所有方式均失败）: {subject[:50]}")
    logger.info(f"📄 报告已保存到本地: {local_path}")
    _send_toast_notification(subject, local_path)
    return False


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    load_dotenv(PROJECT_ROOT / '.env', override=False)
    _ENV_LOADED = True


def _prefer_thumbnail_variant(url: str) -> str:
    """Downsize signed source images before embedding them into email."""
    return (
        str(url or '')
        .replace('w_800%2Ch_800', 'w_120%2Ch_120')
        .replace('w_800,h_800', 'w_120,h_120')
    )


def _inline_remote_images(html_body: str, max_images: int = 24):
    """Convert remote <img> URLs into CID inline attachments for better email rendering."""
    if not html_body:
        return html_body, []

    replacements = {}
    inline_parts = []

    for match in IMG_SRC_PATTERN.finditer(html_body):
        original_url = match.group(2)
        if original_url in replacements:
            continue
        if len(replacements) >= max_images:
            break

        fetch_url = _prefer_thumbnail_variant(original_url)
        try:
            resp = requests.get(fetch_url, timeout=20, verify=False)
            if resp.status_code != 200 or not resp.content:
                continue

            content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if not content_type.startswith('image/'):
                guessed, _ = mimetypes.guess_type(fetch_url)
                content_type = guessed or 'image/jpeg'
            subtype = content_type.split('/')[-1] or 'jpeg'
            if subtype == 'jpg':
                subtype = 'jpeg'

            cid = f'inline-image-{len(inline_parts) + 1}'
            image_part = MIMEImage(resp.content, _subtype=subtype)
            image_part.add_header('Content-ID', f'<{cid}>')
            image_part.add_header('Content-Disposition', 'inline', filename=f'{cid}.{subtype}')
            inline_parts.append(image_part)
            replacements[original_url] = f'cid:{cid}'
        except Exception as e:
            logger.warning(f"内嵌邮件图片失败: {fetch_url[:120]} ({e})")

    if replacements:
        def _replace(match_obj):
            src = match_obj.group(2)
            return f"{match_obj.group(1)}{replacements.get(src, src)}{match_obj.group(3)}"

        html_body = IMG_SRC_PATTERN.sub(_replace, html_body)

    return html_body, inline_parts


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------
def _build_mime_message(
    sender: str,
    to_email: str,
    subject: str,
    html_body: str,
    attachments: Optional[List[str]],
) -> MIMEMultipart:
    """构建 MIME 邮件对象"""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    # HTML 正文 + 内嵌图片
    html_body, inline_parts = _inline_remote_images(html_body)
    related_part = MIMEMultipart("related")
    html_part = MIMEMultipart("alternative")
    html_part.attach(MIMEText(html_body, "html", "utf-8"))
    related_part.attach(html_part)
    for image_part in inline_parts:
        related_part.attach(image_part)
    msg.attach(related_part)

    # 附件
    if attachments:
        for filepath in attachments:
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{os.path.basename(filepath)}"',
                    )
                    msg.attach(part)
            except Exception as e:
                logger.warning(f"附件 {filepath} 添加失败: {e}")

    return msg


def _send_via_smtp(
    host: str,
    port: int,
    use_ssl: bool,
    use_starttls: bool,
    sender: str,
    password: str,
    to_email: str,
    msg: MIMEMultipart,
) -> None:
    """
    通过指定的 SMTP 服务器发送邮件。
    成功则静默返回；失败则抛出异常供调用方处理。
    """
    ctx = ssl.create_default_context()
    local_hostname = _smtp_local_hostname()

    if use_ssl:
        server = smtplib.SMTP_SSL(
            host, port, timeout=30, context=ctx, local_hostname=local_hostname
        )
    else:
        server = smtplib.SMTP(host, port, timeout=30, local_hostname=local_hostname)
        if use_starttls:
            server.ehlo(local_hostname)
            server.starttls(context=ctx)

    try:
        ehlo_code, _resp = server.ehlo(local_hostname)
        if ehlo_code >= 400:
            server.helo(local_hostname)
        # 163 advertises PLAIN but rejects the initial AUTH PLAIN blob, and
        # after a failed EHLO it advertises nothing. Force LOGIN either way.
        if host.endswith("163.com") or not server.esmtp_features.get("auth"):
            server.esmtp_features["auth"] = "LOGIN"
        server.login(sender, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 本地报告 & 桌面通知
# ---------------------------------------------------------------------------
def _save_local_report(subject: str, html_body: str) -> str:
    """保存报告为本地 HTML 文件，返回文件路径"""
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = reports_dir / f"daily_report_{ts}.html"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_body)

    _purge_old_email_report_artifacts(reports_dir)
    logger.info(f"📄 本地报告已保存: {filepath}")
    return str(filepath)


def _purge_old_email_report_artifacts(
    reports_dir: Path,
    keep_days: int = EMAIL_REPORT_RETENTION_DAYS,
    now: Optional[datetime] = None,
) -> int:
    """清理 reports/ 下超过 keep_days 的邮件侧报告产物。"""
    if keep_days < 0 or not reports_dir.exists():
        return 0

    removed = purge_named_artifacts(
        reports_dir,
        EMAIL_REPORT_PATTERNS,
        keep_days,
        now=now or datetime.now(),
    )

    if removed:
        logger.info(f"🧹 已清理 {removed} 份超过 {keep_days} 天的邮件报告产物")
    return removed


def _send_toast_notification(subject: str, report_path: str) -> None:
    """发送 Windows 桌面通知（静默失败，不影响主流程）"""
    try:
        from subprocess import Popen

        escaped_subject = subject[:80].replace("'", "''").replace('"', '&quot;')
        escaped_file = os.path.basename(report_path).replace("'", "''")
        ps_script = (
            '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; '
            '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; '
            '$x = New-Object Windows.Data.Xml.Dom.XmlDocument; '
            f'$x.LoadXml(\'<toast><visual><binding template="ToastGeneric">'
            f'<text>Dajian Listing Tool</text>'
            f'<text>{escaped_subject}</text>'
            f'<text>报告已保存: {escaped_file}</text>'
            f'</binding></visual></toast>\'); '
            '$t = [Windows.UI.Notifications.ToastNotification]::new($x); '
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Dajian Listing Tool").Show($t)'
        )
        Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        logger.info("🔔 桌面通知已发送")
    except Exception as e:
        logger.debug(f"桌面通知发送失败（可忽略）: {e}")
