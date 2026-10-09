"""价格守门员当日活动解析 (P2, 2026-05).

从今天的 scheduler/inventory_sync 日志中扫描 PRICE GUARD / AD AUTO-OFF 标记,
以及最新一次 ad_restore_audit 报告, 汇总出守门员的每日动作:
  - 拒绝改价次数 (`🚫 [PRICE GUARD]`)
  - 自动关广告次数 (`🛑 [AD AUTO-OFF]`)
  - 自动开广告次数 / 拒绝开广告次数 (来自 ad_restore_audit 当天 json)

被 `daily_tasks.send_daily_summary_email` 调用, 在汇总邮件最末尾插入
「价格守门员当日活动」章节. 没有任何动作时返回空字符串 (不污染邮件).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / 'logs'

# 日志文件名 (按需扩展)
_LOG_FILES = [
    'scheduler.log',
    'inventory_sync.log',
    'ad_restore_audit.log',
    'batch_smart_reprice.log',
]

_RX_REJECT = re.compile(r"🚫 \[PRICE GUARD\] (\S+):", re.UNICODE)
_RX_AD_OFF = re.compile(r"🛑 \[AD AUTO-OFF\] (\S+) ", re.UNICODE)
_RX_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")  # 行首 YYYY-MM-DD


def _today_str() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _scan_log_file(path: Path, today: str) -> Dict[str, List[str]]:
    """返回 {'rejects': [sku,...], 'ad_offs': [sku,...]} (今日, 去重保序)."""
    rejects, ad_offs = [], []
    if not path.exists():
        return {'rejects': [], 'ad_offs': []}
    try:
        # 日志可能很大, 仅扫最后 20000 行
        lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()[-20000:]
    except Exception:
        return {'rejects': [], 'ad_offs': []}

    seen_r, seen_a = set(), set()
    for line in lines:
        m = _RX_DATE.match(line)
        if m and m.group(1) != today:
            continue  # 仅今日
        # 没有日期前缀的行 (例如 print) 也包含进来 — 同一文件若有今日 lines 通常应当算
        mr = _RX_REJECT.search(line)
        if mr:
            sku = mr.group(1)
            if sku not in seen_r:
                seen_r.add(sku)
                rejects.append(sku)
        ma = _RX_AD_OFF.search(line)
        if ma:
            sku = ma.group(1)
            if sku not in seen_a:
                seen_a.add(sku)
                ad_offs.append(sku)
    return {'rejects': rejects, 'ad_offs': ad_offs}


def _latest_ad_restore_report(today: str) -> Dict[str, Any]:
    """读今天最新的 ad_restore_audit_*.json, 没有就空."""
    if not LOG_DIR.exists():
        return {}
    today_compact = today.replace('-', '')  # YYYYMMDD
    matches = sorted(
        LOG_DIR.glob(f'ad_restore_audit_{today_compact}_*.json'),
        reverse=True,
    )
    if not matches:
        return {}
    try:
        return json.loads(matches[0].read_text(encoding='utf-8'))
    except Exception:
        return {}


def collect_guard_activity(today: str | None = None) -> Dict[str, Any]:
    """汇总今日守门员活动."""
    today = today or _today_str()
    rejects, ad_offs = [], []
    for fname in _LOG_FILES:
        r = _scan_log_file(LOG_DIR / fname, today)
        rejects.extend(s for s in r['rejects'] if s not in rejects)
        ad_offs.extend(s for s in r['ad_offs'] if s not in ad_offs)

    restore_report = _latest_ad_restore_report(today)
    totals = restore_report.get('totals', {}) if restore_report else {}

    return {
        'date': today,
        'rejects': rejects,
        'ad_offs': ad_offs,
        'restore_ok': totals.get('restored_ok', 0),
        'restore_failed': totals.get('restore_failed', 0),
        'restore_skip_low_margin': totals.get('skip_low_margin', 0),
        'restore_skip_unsafe': totals.get('skip_unsafe', 0),
        'restore_evaluated': totals.get('evaluated_not_promoted', 0),
        'has_restore_report': bool(restore_report),
    }


def render_guard_activity_html(activity: Dict[str, Any]) -> str:
    """渲染成 HTML 片段; 当所有计数为 0 且无报告 → 返回空串."""
    n_reject = len(activity.get('rejects', []))
    n_off = len(activity.get('ad_offs', []))
    n_on = activity.get('restore_ok', 0)
    n_on_fail = activity.get('restore_failed', 0)
    n_skip_low = activity.get('restore_skip_low_margin', 0)
    n_skip_unsafe = activity.get('restore_skip_unsafe', 0)

    if not activity.get('has_restore_report') and not n_reject and not n_off:
        return ''  # 完全无活动

    def _list_html(skus: List[str], color: str) -> str:
        if not skus:
            return '<span style="color:#999;">无</span>'
        shown = skus[:20]
        more = len(skus) - len(shown)
        items = ', '.join(f'<span style="color:{color};font-family:monospace;">{s}</span>' for s in shown)
        if more > 0:
            items += f' <span style="color:#666;">… +{more} 更多</span>'
        return items

    rows = f"""
    <tr style="background:#fef3f2;">
      <td style="padding:8px;border:1px solid #ddd;">🚫 拒绝改价 (低于死线)</td>
      <td style="padding:8px;border:1px solid #ddd;color:#b42318;font-weight:bold;text-align:right;">{n_reject}</td>
    </tr>
    <tr style="background:#fff7ed;">
      <td style="padding:8px;border:1px solid #ddd;">🛑 触底自动关广告</td>
      <td style="padding:8px;border:1px solid #ddd;color:#9a3412;font-weight:bold;text-align:right;">{n_off}</td>
    </tr>
    <tr style="background:#ecfdf3;">
      <td style="padding:8px;border:1px solid #ddd;">✅ 自动恢复广告 (条件改善)</td>
      <td style="padding:8px;border:1px solid #ddd;color:#067647;font-weight:bold;text-align:right;">{n_on}</td>
    </tr>
    <tr>
      <td style="padding:8px;border:1px solid #ddd;color:#666;">  ↳ 恢复失败</td>
      <td style="padding:8px;border:1px solid #ddd;color:#b42318;text-align:right;">{n_on_fail}</td>
    </tr>
    <tr>
      <td style="padding:8px;border:1px solid #ddd;color:#666;">  ↳ 利润不足跳过</td>
      <td style="padding:8px;border:1px solid #ddd;text-align:right;">{n_skip_low}</td>
    </tr>
    <tr>
      <td style="padding:8px;border:1px solid #ddd;color:#666;">  ↳ 现价不安全跳过</td>
      <td style="padding:8px;border:1px solid #ddd;color:#9a3412;text-align:right;">{n_skip_unsafe}</td>
    </tr>
    """

    detail_blocks = []
    if activity.get('rejects'):
        detail_blocks.append(
            f'<details><summary>🚫 被拒改价 SKU ({n_reject})</summary>'
            f'<p style="font-size:13px;line-height:1.7;">{_list_html(activity["rejects"], "#b42318")}</p></details>'
        )
    if activity.get('ad_offs'):
        detail_blocks.append(
            f'<details><summary>🛑 触底关广告 SKU ({n_off})</summary>'
            f'<p style="font-size:13px;line-height:1.7;">{_list_html(activity["ad_offs"], "#9a3412")}</p></details>'
        )

    return f"""
    <h3>5️⃣ 价格守门员当日活动</h3>
    <p style="color:#667085;font-size:12px;margin:4px 0 8px;">
      防亏本死线 + 广告自适应 (5% AD_RATE 不钉死) 联动结果. 评估 SKU 总数: {activity.get('restore_evaluated', '—')}
    </p>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">{rows}</table>
    {''.join(detail_blocks)}
    """
