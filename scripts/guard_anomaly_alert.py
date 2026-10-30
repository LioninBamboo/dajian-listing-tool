"""守门员异常告警 (P8, 2026-05).

每天 09:45 (在 daily_tasks 09:30 之后) 跑一次:
  1. 用 guard_activity.collect_guard_activity() 拿今日 reject + ad_off 计数
  2. 取过去 7 天的同类计数 (从历史 logs 推算)
  3. 若今日数量 ≥ baseline 均值 × ALERT_FACTOR (default 3.0) 且 ≥ MIN_COUNT (5)
     → 立即发独立告警邮件
  4. 同时支持 absolute threshold: reject ≥ ABS_REJECT 或 ad_off ≥ ABS_AD_OFF
     直接告警

历史窗口策略: 扫 logs/scheduler.log 的 6 天 grep 计数 (粗粒度但足够告警判断).
报告: logs/guard_anomaly_<date>.json (一天一份, 当日已告警则 skip)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)
LOG_DIR = PROJECT_ROOT / 'logs'

ALERT_FACTOR = 3.0   # 当日 >= 7 日均值 × 该倍数 → 告警
MIN_COUNT = 5        # 当日至少这个绝对数才告警 (避免低基数噪音)
ABS_REJECT = 30      # 当日 reject 绝对阈值
ABS_AD_OFF = 20      # 当日关广告绝对阈值
HISTORY_DAYS = 7

_RX_REJECT = re.compile(r"🚫 \[PRICE GUARD\] (\S+):", re.UNICODE)
_RX_AD_OFF = re.compile(r"🛑 \[AD AUTO-OFF\] (\S+) ", re.UNICODE)
_RX_LINE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _count_per_day_from_log(path: Path, days: List[str]) -> Dict[str, Dict[str, int]]:
    """对每个日期返回 {'rejects': N, 'ad_offs': N} (按 SKU 去重)."""
    out: Dict[str, Dict[str, set]] = {d: {'rejects': set(), 'ad_offs': set()} for d in days}
    if not path.exists():
        return {d: {'rejects': 0, 'ad_offs': 0} for d in days}
    try:
        lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()[-100000:]
    except Exception:
        return {d: {'rejects': 0, 'ad_offs': 0} for d in days}
    for line in lines:
        m = _RX_LINE_DATE.match(line)
        if not m or m.group(1) not in out:
            continue
        d = m.group(1)
        mr = _RX_REJECT.search(line)
        if mr:
            out[d]['rejects'].add(mr.group(1))
        ma = _RX_AD_OFF.search(line)
        if ma:
            out[d]['ad_offs'].add(ma.group(1))
    return {d: {'rejects': len(v['rejects']), 'ad_offs': len(v['ad_offs'])} for d, v in out.items()}


def collect_history(now: Optional[datetime] = None,
                    history_days: int = HISTORY_DAYS,
                    log_dir: Optional[Path] = None) -> Dict[str, Dict[str, int]]:
    """返回 {date: {'rejects', 'ad_offs'}} 含今天."""
    now = now or datetime.now()
    log_dir = log_dir or LOG_DIR
    days = [(now - timedelta(days=i)).strftime('%Y-%m-%d')
            for i in range(history_days + 1)]  # 今天 + 前 N 天

    # 累计扫多个日志文件
    combined = {d: {'rejects': set(), 'ad_offs': set()} for d in days}
    for fname in ['scheduler.log', 'inventory_sync.log', 'batch_smart_reprice.log']:
        per_day = _count_per_day_from_log(log_dir / fname, days)
        # 把 sku 合并 — 但 _count_per_day_from_log 已聚合, 这里只取 max
        # (不同文件的同 SKU 计为 1 即可) — 简化: 取最大值
        for d in days:
            combined[d]['rejects'] = max(
                combined[d]['rejects'] if isinstance(combined[d]['rejects'], int) else 0,
                per_day[d]['rejects'],
            ) if isinstance(combined[d]['rejects'], int) else per_day[d]['rejects']
            combined[d]['ad_offs'] = max(
                combined[d]['ad_offs'] if isinstance(combined[d]['ad_offs'], int) else 0,
                per_day[d]['ad_offs'],
            ) if isinstance(combined[d]['ad_offs'], int) else per_day[d]['ad_offs']
    # 上面 set/int 处理粗糙 — 重新简化:
    return {d: {'rejects': int(combined[d]['rejects']) if isinstance(combined[d]['rejects'], int)
                          else len(combined[d]['rejects']),
                'ad_offs': int(combined[d]['ad_offs']) if isinstance(combined[d]['ad_offs'], int)
                           else len(combined[d]['ad_offs'])}
            for d in days}


def evaluate_anomaly(history: Dict[str, Dict[str, int]],
                      today: Optional[str] = None,
                      alert_factor: float = ALERT_FACTOR,
                      min_count: int = MIN_COUNT,
                      abs_reject: int = ABS_REJECT,
                      abs_ad_off: int = ABS_AD_OFF) -> Dict[str, Any]:
    """评估今日是否异常.

    Returns:
        {today_rejects, today_ad_offs, baseline_avg_rejects, baseline_avg_ad_offs,
         is_anomaly, triggers: [...]}
    """
    today = today or datetime.now().strftime('%Y-%m-%d')
    today_data = history.get(today, {'rejects': 0, 'ad_offs': 0})
    baseline = [v for k, v in history.items() if k != today]
    n = len(baseline) or 1
    avg_rej = sum(b['rejects'] for b in baseline) / n
    avg_off = sum(b['ad_offs'] for b in baseline) / n

    triggers = []
    if today_data['rejects'] >= min_count and today_data['rejects'] >= avg_rej * alert_factor and avg_rej > 0:
        triggers.append(f"reject 数 {today_data['rejects']} ≥ 7日均值 {avg_rej:.1f} × {alert_factor}")
    if today_data['ad_offs'] >= min_count and today_data['ad_offs'] >= avg_off * alert_factor and avg_off > 0:
        triggers.append(f"ad_off 数 {today_data['ad_offs']} ≥ 7日均值 {avg_off:.1f} × {alert_factor}")
    if today_data['rejects'] >= abs_reject:
        triggers.append(f"reject 数 {today_data['rejects']} ≥ 绝对阈值 {abs_reject}")
    if today_data['ad_offs'] >= abs_ad_off:
        triggers.append(f"ad_off 数 {today_data['ad_offs']} ≥ 绝对阈值 {abs_ad_off}")

    return {
        'today': today,
        'today_rejects': today_data['rejects'],
        'today_ad_offs': today_data['ad_offs'],
        'baseline_avg_rejects': round(avg_rej, 1),
        'baseline_avg_ad_offs': round(avg_off, 1),
        'is_anomaly': bool(triggers),
        'triggers': triggers,
        'history': history,
    }


def _already_alerted_today(today: str, log_dir: Path) -> bool:
    return (log_dir / f'guard_anomaly_{today}.json').exists()


def run(send_email: bool = True, force: bool = False,
        log_dir: Optional[Path] = None) -> Dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    log_dir = log_dir or LOG_DIR
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')

    if not force and _already_alerted_today(today, log_dir):
        logger.info("今日已告警过, 跳过 (用 --force 强制)")
        return {'skipped': True, 'today': today}

    history = collect_history(now, log_dir=log_dir)
    eva = evaluate_anomaly(history, today=today)

    if eva['is_anomaly']:
        log_dir.mkdir(parents=True, exist_ok=True)
        out = log_dir / f'guard_anomaly_{today}.json'
        out.write_text(json.dumps(eva, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.warning(f"⚠️ 守门员异常 ({len(eva['triggers'])} 个触发器), 报告: {out}")
        if send_email:
            try:
                from src.utils.email_sender import send_email as _send
                html = _render_alert_html(eva)
                _send(f"⚠️ 守门员异常告警 - {today}", html)
            except Exception as exc:
                logger.warning(f"告警邮件发送失败: {exc}")
    else:
        logger.info(f"守门员当日正常 (rejects={eva['today_rejects']}, ad_offs={eva['today_ad_offs']})")

    return eva


def _render_alert_html(eva: Dict[str, Any]) -> str:
    triggers_html = ''.join(f'<li>{t}</li>' for t in eva['triggers'])
    history_rows = ''
    for d in sorted(eva['history'].keys(), reverse=True):
        v = eva['history'][d]
        bg = '#fef3f2' if d == eva['today'] else '#fff'
        history_rows += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:4px 8px;border:1px solid #ddd;">{d}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">{v["rejects"]}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">{v["ad_offs"]}</td>'
            f'</tr>'
        )
    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:680px;">
    <h2 style="color:#b42318;">⚠️ 守门员异常告警 — {eva['today']}</h2>
    <p>当日 reject={eva['today_rejects']}, ad_off={eva['today_ad_offs']}.
       7 日均值 reject={eva['baseline_avg_rejects']}, ad_off={eva['baseline_avg_ad_offs']}.</p>
    <h3>触发原因</h3>
    <ul>{triggers_html}</ul>
    <h3>近 8 天历史</h3>
    <table style="border-collapse:collapse;font-size:13px;">
      <tr style="background:#f5f5f5;">
        <th style="padding:4px 8px;border:1px solid #ddd;">日期</th>
        <th style="padding:4px 8px;border:1px solid #ddd;">拒改价</th>
        <th style="padding:4px 8px;border:1px solid #ddd;">关广告</th>
      </tr>
      {history_rows}
    </table>
    <p style="color:#666;font-size:12px;margin-top:16px;">
       请检查供应商成本是否有批量上调, 或市场是否进入价格战. 必要时人工介入.
    </p>
    </body></html>"""


def _parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-email', action='store_true')
    ap.add_argument('--force', action='store_true', help='今日已告警也强制再发一次')
    return ap.parse_args()


if __name__ == '__main__':
    a = _parse_args()
    r = run(send_email=not a.no_email, force=a.force)
    print(json.dumps({k: v for k, v in r.items() if k != 'history'}, ensure_ascii=False, indent=2))
