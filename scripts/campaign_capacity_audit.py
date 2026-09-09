"""eBay PLA Campaign 容量负载均衡器 (P10, 2026-05).

eBay Promoted Listings 单 campaign 上限 ~10,000 条 listing.
本脚本:
  1. 拉所有 RUNNING campaign + 每个 campaign 的 ad 数
  2. 标记 "接近上限" (>= warn_threshold, 默认 9000) 的 campaign
  3. 标记 "低填充" (< low_threshold, 默认 1000) 可作为迁移目的地
  4. 输出报告 logs/campaign_capacity_<ts>.json
  5. 触发条件邮件告警 (任意 campaign 接近上限)

不自动迁移 ad — 迁移需 delete + create_ad, 风险较高, 留给人工或后续 slice.

CLI:
    python scripts/campaign_capacity_audit.py
    python scripts/campaign_capacity_audit.py --email
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)
LOG_DIR = PROJECT_ROOT / 'logs'

# eBay PLA single-campaign 软上限 (官方文档值)
EBAY_CAMPAIGN_AD_LIMIT = 10_000
WARN_THRESHOLD = 9_000   # >= 此数 → 告警
LOW_THRESHOLD = 1_000    # < 此数 → 可作为迁入目的地


def collect_capacity(ad_service=None) -> Dict[str, Any]:
    """返回 {campaigns: [{id, name, ad_count, capacity, fill_pct, status}], saturated, available}."""
    if ad_service is None:
        from src.services.ebay_ad_service import EbayAdService
        ad_service = EbayAdService()

    campaigns_raw = ad_service.fetch_campaigns(status='RUNNING') or []
    campaigns: List[Dict[str, Any]] = []
    for c in campaigns_raw:
        cid = c.get('campaignId') or c.get('campaign_id')
        if not cid:
            continue
        try:
            ads = ad_service.fetch_campaign_ads(cid) or []
        except Exception as exc:
            logger.warning(f"campaign {cid} fetch_campaign_ads 失败: {exc}")
            ads = []
        ad_count = len(ads)
        fill_pct = ad_count / EBAY_CAMPAIGN_AD_LIMIT
        if ad_count >= WARN_THRESHOLD:
            status = 'saturated'
        elif ad_count < LOW_THRESHOLD:
            status = 'available'
        else:
            status = 'normal'
        campaigns.append({
            'campaign_id': cid,
            'name': c.get('campaignName') or c.get('name') or cid,
            'ad_count': ad_count,
            'capacity': EBAY_CAMPAIGN_AD_LIMIT,
            'fill_pct': round(fill_pct, 4),
            'status': status,
        })

    saturated = [c for c in campaigns if c['status'] == 'saturated']
    available = [c for c in campaigns if c['status'] == 'available']
    total_ads = sum(c['ad_count'] for c in campaigns)
    total_capacity = len(campaigns) * EBAY_CAMPAIGN_AD_LIMIT

    return {
        'timestamp': datetime.now().isoformat(),
        'campaigns': sorted(campaigns, key=lambda x: -x['ad_count']),
        'saturated': saturated,
        'available': available,
        'total_ads': total_ads,
        'total_capacity': total_capacity,
        'global_fill_pct': round(total_ads / total_capacity, 4) if total_capacity else 0,
    }


def render_html(report: Dict[str, Any]) -> str:
    rows = ''
    for c in report['campaigns']:
        color = ('#b42318' if c['status'] == 'saturated' else
                 '#067647' if c['status'] == 'available' else '#999')
        rows += (
            f'<tr><td style="padding:4px 8px;border:1px solid #ddd;font-family:monospace;">{c["campaign_id"]}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;">{c["name"][:40]}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">{c["ad_count"]:,}</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;text-align:right;">{c["fill_pct"]*100:.1f}%</td>'
            f'<td style="padding:4px 8px;border:1px solid #ddd;color:{color};">{c["status"]}</td></tr>'
        )
    color_global = '#b42318' if report['saturated'] else '#067647'
    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;max-width:780px;">
    <h2 style="color:{color_global};">📊 PLA Campaign 容量报告 — {report['timestamp'][:19].replace('T',' ')}</h2>
    <p>共 {len(report['campaigns'])} 个 campaign, 累计 {report['total_ads']:,} 个 ad
       ({report['global_fill_pct']*100:.1f}%).
       <b>饱和 {len(report['saturated'])} 个</b>,
       低填充可迁入 {len(report['available'])} 个.</p>
    <table style="border-collapse:collapse;font-size:13px;">
      <tr style="background:#f5f5f5;">
        <th style="padding:6px;border:1px solid #ddd;">Campaign ID</th>
        <th style="padding:6px;border:1px solid #ddd;">名称</th>
        <th style="padding:6px;border:1px solid #ddd;">广告数</th>
        <th style="padding:6px;border:1px solid #ddd;">填充率</th>
        <th style="padding:6px;border:1px solid #ddd;">状态</th>
      </tr>
      {rows or '<tr><td colspan=5 style="padding:8px;color:#999;">无数据</td></tr>'}
    </table>
    <p style="color:#666;font-size:12px;margin-top:16px;">
       饱和 campaign 接近 eBay 单 campaign {EBAY_CAMPAIGN_AD_LIMIT:,} 上限,
       建议在 eBay 后台手动新建 campaign 或将部分 ad 迁移至低填充 campaign.
    </p>
    </body></html>"""


def run(send_email: bool = False) -> Dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    report = collect_capacity()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / f"campaign_capacity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"报告: {out} | campaigns={len(report['campaigns'])} "
                f"saturated={len(report['saturated'])} available={len(report['available'])}")

    if send_email and report['saturated']:
        try:
            from src.utils.email_sender import send_email as _send
            subject = f"⚠️ PLA Campaign 容量告警 - {len(report['saturated'])} 个接近上限"
            _send(subject, render_html(report))
        except Exception as exc:
            logger.warning(f"邮件发送失败: {exc}")
    return report


def _parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--email', action='store_true')
    return ap.parse_args()


if __name__ == '__main__':
    a = _parse_args()
    r = run(send_email=a.email)
    print(f"\nDONE — campaigns={len(r['campaigns'])} saturated={len(r['saturated'])} "
          f"available={len(r['available'])} fill={r['global_fill_pct']*100:.1f}%")
