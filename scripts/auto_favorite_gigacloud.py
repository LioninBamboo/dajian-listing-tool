"""
GigaCloud 自动收藏脚本 v2
通过 CDP 连接真实 Chrome，用正确的产品页面 URL 点击心形按钮

关键发现:
    - 产品页面 URL: product/product&product_id=XXX (不是 buyer/product/detail&sku=)
    - 心形按钮: button.btn-wishlist
    - 未收藏: 内部 <i> 有 class "gc-like"
    - 已收藏: 内部 <i> 有 class "gc-like-full"
    - SKU→product_id: 通过搜索页面 product/search&search=SKU 获取

用法:
    python auto_favorite_gigacloud.py [--reset]
"""
import sys
import os
import time
import json
import logging
import subprocess
import shutil
import random
from pathlib import Path
from datetime import datetime

# 设置日志
os.makedirs('logs', exist_ok=True)
log_file = f'logs/auto_favorite_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# GigaCloud URL
GIGACLOUD_BASE = "https://www.gigab2b.com"
SEARCH_URL_TEMPLATE = GIGACLOUD_BASE + "/index.php?route=product/search&search={sku}"
PRODUCT_URL_TEMPLATE = GIGACLOUD_BASE + "/index.php?route=product/product&product_id={pid}"

# Chrome
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
CDP_PORT = 9222

# 缓存: SKU → product_id
SKU_MAP_FILE = 'cache/sku_product_id_map.json'


def find_chrome():
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    return shutil.which("chrome")


def launch_chrome():
    chrome_path = find_chrome()
    if not chrome_path:
        logger.error("找不到 Chrome！")
        sys.exit(1)

    user_data_dir = str(Path(__file__).parent / ".chrome_profile_gigacloud")
    os.makedirs(user_data_dir, exist_ok=True)

    logger.info(f"启动 Chrome: {chrome_path}")
    proc = subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1400,900",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    return proc


def load_skus(filepath='cache/unfavorited_skus.txt'):
    skus = []
    with open(filepath, 'r') as f:
        for line in f:
            sku = line.strip()
            if sku:
                skus.append(sku)
    return skus


def save_progress(favorited, failed, skipped, filepath='cache/favorite_progress.json'):
    with open(filepath, 'w') as f:
        json.dump({
            'favorited': favorited,
            'failed': failed,
            'skipped': skipped,
            'timestamp': datetime.now().isoformat()
        }, f, indent=2, ensure_ascii=False)


def load_progress(filepath='cache/favorite_progress.json'):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return {'favorited': [], 'failed': [], 'skipped': []}


def load_sku_map():
    if os.path.exists(SKU_MAP_FILE):
        with open(SKU_MAP_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_sku_map(m):
    with open(SKU_MAP_FILE, 'w') as f:
        json.dump(m, f, indent=2)


def do_login(page):
    """登录 GigaCloud"""
    page.goto(f"{GIGACLOUD_BASE}/index.php?route=customer/login", timeout=30000)
    time.sleep(3)

    if 'login' not in page.url:
        logger.info("已登录")
        return True

    logger.info("正在登录...")

    # 关闭 cookie 弹窗
    for sel in ['button:has-text("Accept")', '.cookie-accept']:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(0.5)
        except:
            pass

    time.sleep(1)

    # 填写邮箱
    email_input = page.query_selector('input[name="email"], input[type="email"], #input-email')
    if email_input:
        email_input.click()
        time.sleep(0.3)
        email_input.fill('')
        gc_email = os.getenv('GIGACLOUD_EMAIL', '')
        if not gc_email:
            logger.error('GIGACLOUD_EMAIL not set in .env')
            return False
        page.keyboard.type(gc_email, delay=50)

    time.sleep(0.5)

    # 填写密码
    pwd_input = page.query_selector('input[name="password"], input[type="password"], #input-password')
    if pwd_input:
        pwd_input.click()
        time.sleep(0.3)
        pwd_input.fill('')
        gc_pwd = os.getenv('GIGACLOUD_PASSWORD', '')
        if not gc_pwd:
            logger.error('GIGACLOUD_PASSWORD not set in .env')
            return False
        page.keyboard.type(gc_pwd, delay=50)

    time.sleep(1)

    # 点击登录
    login_btn = page.query_selector('button[type="submit"], input[type="submit"]')
    if login_btn:
        login_btn.click()

    # 等待登录
    for i in range(120):
        time.sleep(1)
        if 'login' not in page.url:
            logger.info("登录成功！")
            return True
        if i % 30 == 0 and i > 0:
            logger.info(f"等待登录... ({i}s)")

    logger.error("登录超时")
    return False


def search_product_id(page, sku):
    """通过搜索页面获取 SKU 对应的 product_id"""
    url = SEARCH_URL_TEMPLATE.format(sku=sku)
    page.goto(url, timeout=30000, wait_until='domcontentloaded')
    time.sleep(2)

    # 提取搜索结果中的 product_id 链接
    links = page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('a[href*="product_id"]').forEach(a => {
            const match = a.href.match(/product_id=(\\d+)/);
            if (match) {
                results.push(match[1]);
            }
        });
        // 去重
        return [...new Set(results)];
    }""")

    if links:
        return links[0]
    return None


def click_wishlist(page):
    """
    点击心形收藏按钮
    返回: 'clicked' | 'already_favorited' | 'not_found'
    """
    result = page.evaluate("""() => {
        // 找 btn-wishlist 按钮 (多种选择器)
        let btn = document.querySelector('button.btn-wishlist');
        if (!btn) btn = document.querySelector('.btn-wishlist');
        if (!btn) btn = document.querySelector('[class*="btn-wishlist"]');
        // 备选: 找包含 gc-like 图标的按钮
        if (!btn) {
            const icons = document.querySelectorAll('i.gc-like, i[class*="gc-like"]');
            for (const icon of icons) {
                const parent = icon.closest('button') || icon.parentElement;
                if (parent && parent.offsetParent) {
                    const r = parent.getBoundingClientRect();
                    // 在可见区域内的 (y < 800)
                    if (r.y > 0 && r.y < 800 && r.width > 10) {
                        btn = parent;
                        break;
                    }
                }
            }
        }
        if (!btn) return 'not_found';

        // 检查内部图标是否已收藏
        const icon = btn.querySelector('i');
        if (icon) {
            const cls = icon.className || '';
            if (cls.includes('gc-like-full')) {
                return 'already_favorited';
            }
        }

        // 检查父容器 active-wishlist
        let parent = btn.parentElement;
        for (let i = 0; i < 3 && parent; i++) {
            if (parent.className && parent.className.includes('active-wishlist')) {
                return 'already_favorited';
            }
            parent = parent.parentElement;
        }

        // 点击
        btn.click();
        return 'clicked';
    }""")

    return result


def dismiss_popup(page):
    """关闭收藏群组弹窗"""
    time.sleep(1)
    try:
        page.mouse.click(700, 500)
        time.sleep(0.5)
        page.keyboard.press('Escape')
        time.sleep(0.3)
    except:
        pass


def verify_favorited(page):
    """验证产品是否已收藏"""
    return page.evaluate("""() => {
        const btn = document.querySelector('button.btn-wishlist');
        if (!btn) return false;
        const icon = btn.querySelector('i');
        if (icon && icon.className.includes('gc-like-full')) return true;
        let parent = btn.parentElement;
        for (let i = 0; i < 3 && parent; i++) {
            if (parent.className && parent.className.includes('active-wishlist')) return true;
            parent = parent.parentElement;
        }
        return false;
    }""")


def main():
    from playwright.sync_api import sync_playwright

    # --reset 参数
    if '--reset' in sys.argv:
        logger.info("重置进度文件...")
        for f in ['cache/favorite_progress.json']:
            if os.path.exists(f):
                os.remove(f)

    # 加载 SKU
    skus = load_skus()
    logger.info(f"共 {len(skus)} 个 SKU 需要收藏")

    # 加载进度
    progress = load_progress()
    favorited = list(progress.get('favorited', []))
    failed_before = list(progress.get('failed', []))
    skipped = list(progress.get('skipped', []))

    # 只跳过已成功和已跳过的; 之前失败的允许重试
    done_skus = set(favorited + skipped)
    remaining = [s for s in skus if s not in done_skus]

    if done_skus:
        logger.info(f"已成功: {len(favorited)}, 已跳过: {len(skipped)}, "
                    f"之前失败(将重试): {len(failed_before)}")

    if not remaining:
        logger.info("所有 SKU 已处理完毕！")
        logger.info(f"  成功: {len(favorited)}, 跳过: {len(skipped)}")
        return

    logger.info(f"待处理: {len(remaining)} 个")

    # 重置 failed 列表
    failed = []

    # SKU→product_id 缓存
    sku_map = load_sku_map()

    # 启动 Chrome
    chrome_proc = launch_chrome()

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}", timeout=30000)
            logger.info("已连接到 Chrome")
        except Exception as e:
            logger.error(f"无法连接: {e}")
            logger.info("尝试重新启动 Chrome...")
            try:
                chrome_proc.terminate()
            except:
                pass
            time.sleep(3)
            chrome_proc = launch_chrome()
            time.sleep(5)
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}", timeout=30000)
                logger.info("第二次连接成功")
            except Exception as e2:
                logger.error(f"仍然无法连接: {e2}")
                chrome_proc.terminate()
                return

        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 登录
        if not do_login(page):
            logger.error("登录失败，请手动登录后重新运行脚本")
            browser.close()
            chrome_proc.terminate()
            return

        # 稳定会话: 登录后访问一个产品页面做 warm-up
        time.sleep(2)
        try:
            page.goto(f"{GIGACLOUD_BASE}/index.php?route=account/wishlist", timeout=15000, wait_until='domcontentloaded')
            time.sleep(3)
            if 'login' in page.url:
                logger.warning("会话不稳定，等待再试...")
                time.sleep(5)
                do_login(page)
                time.sleep(3)
            else:
                logger.info("已登录")
        except:
            pass
        time.sleep(2)

        logger.info(f"\n{'='*60}")
        logger.info(f"开始批量收藏 {len(remaining)} 个 SKU")
        logger.info(f"{'='*60}\n")

        consecutive_errors = 0

        for idx, sku in enumerate(remaining):
            try:
                logger.info(f"[{idx+1}/{len(remaining)}] SKU: {sku}")

                # 第1步: 获取 product_id
                pid = sku_map.get(sku)

                if not pid:
                    logger.info(f"  搜索 product_id...")
                    pid = search_product_id(page, sku)

                    if not pid:
                        logger.warning(f"  搜索不到此 SKU 的产品页面")
                        failed.append(sku)
                        save_progress(favorited, failed, skipped)
                        time.sleep(1)
                        continue

                    sku_map[sku] = pid
                    save_sku_map(sku_map)
                    logger.info(f"  找到 product_id: {pid}")
                    time.sleep(1)

                # 第2步: 打开产品页面
                product_url = PRODUCT_URL_TEMPLATE.format(pid=pid)
                page.goto(product_url, timeout=30000, wait_until='networkidle')
                time.sleep(2)

                # 等待按钮出现 (Vue.js 动态渲染)
                try:
                    page.wait_for_selector('button.btn-wishlist, .btn-wishlist, [class*="wishlist"]', timeout=8000)
                except:
                    # 再等一下
                    time.sleep(3)

                # 检查是否被重定向
                if 'product_id' not in page.url:
                    logger.warning(f"  页面被重定向: {page.url}")
                    if 'login' in page.url:
                        logger.info("  需要重新登录...")
                        if do_login(page):
                            # 重新登录后用 networkidle 加载产品页
                            time.sleep(2)
                            page.goto(product_url, timeout=30000, wait_until='networkidle')
                            time.sleep(2)
                            try:
                                page.wait_for_selector('button.btn-wishlist, .btn-wishlist, [class*="wishlist"]', timeout=8000)
                            except:
                                time.sleep(3)
                            if 'product_id' not in page.url:
                                logger.warning(f"  重新登录后仍被重定向: {page.url}")
                                failed.append(sku)
                                save_progress(favorited, failed, skipped)
                                continue
                        else:
                            logger.error("  重新登录失败")
                            failed.append(sku)
                            save_progress(favorited, failed, skipped)
                            continue
                    else:
                        failed.append(sku)
                        save_progress(favorited, failed, skipped)
                        continue

                # 第3步: 点击心形
                result = click_wishlist(page)

                if result == 'clicked':
                    dismiss_popup(page)
                    time.sleep(0.5)

                    if verify_favorited(page):
                        logger.info(f"  ✓ 收藏成功")
                    else:
                        logger.info(f"  ✓ 已点击收藏")
                    favorited.append(sku)
                    consecutive_errors = 0

                elif result == 'already_favorited':
                    logger.info(f"  已收藏，跳过")
                    skipped.append(sku)
                    consecutive_errors = 0

                elif result == 'not_found':
                    # 调试: 打印页面上所有按钮和 wishlist 相关元素
                    debug_info = page.evaluate("""() => {
                        const btns = [];
                        document.querySelectorAll('button').forEach(b => {
                            if (b.offsetParent && b.getBoundingClientRect().y < 800) {
                                btns.push({cls: (b.className||'').substring(0,100), y: Math.round(b.getBoundingClientRect().y)});
                            }
                        });
                        const likes = [];
                        document.querySelectorAll('[class*="like"], [class*="wish"], [class*="collect"]').forEach(el => {
                            if (el.offsetParent) {
                                likes.push({tag: el.tagName, cls: (el.className||'').toString().substring(0,100), y: Math.round(el.getBoundingClientRect().y)});
                            }
                        });
                        return {url: location.href, btns: btns.slice(0,10), likes: likes.slice(0,10)};
                    }""")
                    logger.warning(f"  找不到收藏按钮 URL={debug_info['url']}")
                    logger.warning(f"  页面按钮: {debug_info['btns'][:5]}")
                    logger.warning(f"  Like元素: {debug_info['likes'][:5]}")
                    if idx < 5:
                        page.screenshot(path=f'logs/no_wishlist_{sku}.png', full_page=False)
                    failed.append(sku)
                    consecutive_errors += 1

                save_progress(favorited, failed, skipped)

                # 进度报告
                if (idx + 1) % 10 == 0:
                    logger.info(f"  --- 进度: {idx+1}/{len(remaining)} | "
                              f"成功: {len(favorited)} | 跳过: {len(skipped)} | "
                              f"失败: {len(failed)} ---")

                # 连续错误检测
                if consecutive_errors >= 20:
                    logger.error("连续 20 个错误，停止执行")
                    break

                # 限速
                delay = random.uniform(2, 5)
                time.sleep(delay)

            except Exception as e:
                error_msg = str(e)
                logger.warning(f"  异常: {error_msg[:200]}")

                if 'closed' in error_msg.lower() or 'crash' in error_msg.lower():
                    logger.error("浏览器已关闭！尝试重新连接...")
                    try:
                        chrome_proc = launch_chrome()
                        time.sleep(3)
                        browser = p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}", timeout=30000)
                        ctx = browser.contexts[0]
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()

                        if do_login(page):
                            logger.info("重新连接成功！")
                            consecutive_errors = 0
                        else:
                            logger.error("重新登录失败，停止")
                            break
                    except Exception as e2:
                        logger.error(f"重新连接失败: {e2}")
                        break

                failed.append(sku)
                save_progress(favorited, failed, skipped)
                consecutive_errors += 1
                time.sleep(3)

        # 最终报告
        logger.info(f"\n{'='*60}")
        logger.info(f"收藏完成！")
        logger.info(f"  成功: {len(favorited)}")
        logger.info(f"  已收藏跳过: {len(skipped)}")
        logger.info(f"  失败: {len(failed)}")
        logger.info(f"{'='*60}")

        if failed:
            logger.info(f"\n失败的 SKU ({len(failed)} 个):")
            for s in failed[:20]:
                logger.info(f"  - {s}")
            if len(failed) > 20:
                logger.info(f"  ... 及其他 {len(failed)-20} 个")

        save_progress(favorited, failed, skipped)

        try:
            browser.close()
        except:
            pass
        try:
            chrome_proc.terminate()
        except:
            pass


if __name__ == '__main__':
    main()
