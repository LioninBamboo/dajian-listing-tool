"""S85 — weekly PDF/HTML tests."""
from __future__ import annotations

from src.services.cro_weekly_pdf import (
    render_executive_html, write_html_report,
)


def _sample_report():
    return {
        'week_label': '2026-W18',
        'headline': '本周转化率 +5.2%',
        'lift': {'lift_pct': 0.052, 'sample_per_arm': 200,
                 'significant': True},
        'approvals': {'by_kind': {
            'rollback': {'pending': 3, 'auto_approve': 1},
            'threshold': {'pending': 5, 'auto_approve': 4},
        }},
        'returns_top': [
            {'sku': 'A1', 'returns_pct': 0.18, 'top_reason': 'size'},
            {'sku': 'B2', 'returns_pct': 0.16, 'top_reason': 'quality'},
        ],
        'inventory_alerts': [
            {'sku': 'C3', 'stock': 5, 'days_to_stockout': 6},
        ],
        'drift': {'drift_detected': True, 'drift_weeks': ['W17']},
        'cost': {'today_cost_usd': 1.23, 'avg_cost_per_decision': 0.004,
                 'suggest_downgrade': False},
    }


def test_html_contains_all_sections():
    html = render_executive_html(_sample_report())
    for section in ['A/B Lift', '审批吞吐', '退货 Top',
                    '库存告警', 'Cohort drift', '成本']:
        assert section in html
    assert '2026-W18' in html
    assert '本周转化率' in html


def test_html_handles_empty_collections():
    html = render_executive_html({'week_label': 'W1', 'headline': ''})
    assert 'W1' in html
    # 空 by_kind 走 fallback row
    assert '<table' in html


def test_html_renders_returns_top_skus():
    html = render_executive_html(_sample_report())
    assert 'A1' in html
    assert 'size' in html


def test_html_renders_drift_flag():
    html = render_executive_html(_sample_report())
    assert 'drift_detected' in html
    assert 'True' in html


def test_write_html_report_creates_file(tmp_path):
    out = tmp_path / 'sub' / 'report.html'
    written = write_html_report(_sample_report(), out)
    assert written == out
    assert out.exists()
    assert '<html' in out.read_text(encoding='utf-8')


def test_html_truncates_returns_to_10():
    rep = _sample_report()
    rep['returns_top'] = [{'sku': f'S{i}', 'returns_pct': 0.1,
                            'top_reason': 'x'} for i in range(20)]
    html = render_executive_html(rep)
    assert 'S0' in html
    assert 'S9' in html
    assert 'S15' not in html
