"""S100 — executive natural summary tests."""
from __future__ import annotations

from src.services.cro_executive_natural_summary import (
    _signed, generate_alert_summary, generate_summary, render_template,
)


SAMPLE = {
    'date': '2026-03-30',
    'gmv': 12345.67,
    'gmv_chg_pct': 0.12,
    'orders': 100,
    'cvr': 0.05,
    'ad_spend': 200,
    'acos': 0.18,
    'top_sku': 'SKU-A',
    'low_stock_count': 3,
    'return_rate': 0.02,
    'recommendation': '加推 SKU-A',
}


def test_signed_positive():
    assert _signed(0.12) == '+12.0%'


def test_signed_negative():
    assert _signed(-0.05) == '-5.0%'


def test_signed_none_or_zero():
    assert _signed(None) == '持平'
    assert _signed(0) == '持平'


def test_render_template_contains_key_fields():
    text = render_template(SAMPLE)
    assert 'SKU-A' in text
    assert '12345.67' in text
    assert '+12.0%' in text


def test_render_template_handles_missing_fields():
    text = render_template({})
    assert isinstance(text, str)
    assert '今日' in text


def test_generate_summary_template_when_no_llm():
    out = generate_summary(SAMPLE)
    assert out['source'] == 'template'
    assert 'SKU-A' in out['text']


def test_generate_summary_uses_llm_when_provided():
    out = generate_summary(SAMPLE, llm_call=lambda p: '昨日 GMV 增长.')
    assert out['source'] == 'llm'
    assert out['text'] == '昨日 GMV 增长.'


def test_generate_summary_falls_back_when_llm_empty():
    out = generate_summary(SAMPLE, llm_call=lambda p: '   ')
    assert out['source'] == 'template_empty_llm'
    assert 'SKU-A' in out['text']


def test_generate_summary_falls_back_when_llm_raises():
    def bad(p):
        raise RuntimeError('api down')
    out = generate_summary(SAMPLE, llm_call=bad)
    assert out['source'] == 'template_llm_error'
    assert 'api down' in out['error']
    assert 'SKU-A' in out['text']


def test_generate_alert_summary_empty():
    assert '无异常' in generate_alert_summary([])


def test_generate_alert_summary_truncates_at_three():
    alerts = [
        {'severity': 'CRIT', 'title': 'A'},
        {'severity': 'CRIT', 'title': 'B'},
        {'severity': 'WARN', 'title': 'C'},
        {'severity': 'WARN', 'title': 'D'},
    ]
    text = generate_alert_summary(alerts)
    assert '另有 1 条' in text
