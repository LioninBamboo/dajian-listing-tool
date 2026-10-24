"""S100 — 高管自然语言日报.

输入: 业务报告 dict; 优先 LLM, 失败回退模板.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional


TEMPLATE = (
    '【{date}】今日 GMV ${gmv}, 环比{gmv_chg_str}; '
    '订单 {orders} 单, 转化率 {cvr_pct:.2%}. '
    '推广花费 ${ad_spend}, ACoS {acos_pct:.2%}. '
    'Top SKU: {top_sku}; 库存告警 {low_stock_count} 个; '
    '退货率 {return_pct:.2%}. '
    '建议: {recommendation}.'
)


def _signed(value: float) -> str:
    if value is None:
        return '持平'
    if value > 0:
        return f'+{value:.1%}'
    if value < 0:
        return f'{value:.1%}'
    return '持平'


def render_template(report: Dict[str, Any]) -> str:
    safe = {
        'date': report.get('date', '今日'),
        'gmv': round(float(report.get('gmv', 0) or 0), 2),
        'gmv_chg_str': _signed(report.get('gmv_chg_pct')),
        'orders': int(report.get('orders', 0) or 0),
        'cvr_pct': float(report.get('cvr', 0) or 0),
        'ad_spend': round(float(report.get('ad_spend', 0) or 0), 2),
        'acos_pct': float(report.get('acos', 0) or 0),
        'top_sku': report.get('top_sku', 'N/A'),
        'low_stock_count': int(report.get('low_stock_count', 0) or 0),
        'return_pct': float(report.get('return_rate', 0) or 0),
        'recommendation': report.get('recommendation',
                                     '维持当前节奏, 关注库存与广告 ROI'),
    }
    return TEMPLATE.format(**safe)


def generate_summary(report: Dict[str, Any],
                     *,
                     llm_call: Optional[Callable[[str], str]] = None,
                     ) -> Dict[str, Any]:
    template_text = render_template(report)
    if llm_call is None:
        return {'text': template_text, 'source': 'template'}
    prompt = (
        '请用中文给一段不超过 120 字的高管业务播报, 数据如下:\n'
        f'{report}\n基于事实, 不要编造数字.'
    )
    try:
        text = (llm_call(prompt) or '').strip()
        if not text:
            return {'text': template_text, 'source': 'template_empty_llm'}
        return {'text': text, 'source': 'llm'}
    except Exception as exc:
        return {'text': template_text, 'source': 'template_llm_error',
                'error': str(exc)}


def generate_alert_summary(alerts: list) -> str:
    if not alerts:
        return '今日无异常告警, 业务平稳运行.'
    head = alerts[:3]
    body = '; '.join(
        f"{a.get('severity', 'INFO')}-{a.get('title', '')}" for a in head)
    extra = f' (另有 {len(alerts) - 3} 条)' if len(alerts) > 3 else ''
    return f'今日告警 {len(alerts)} 条: {body}{extra}.'
