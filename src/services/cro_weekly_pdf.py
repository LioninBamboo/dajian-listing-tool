"""S85 — 周报 PDF/HTML 生成.

不强依赖 reportlab; 默认输出纯 HTML (后续可 wkhtmltopdf 转 PDF).
若安装了 reportlab 可用 build_pdf_with_reportlab.
所有数据 fetcher 注入避免 sqlite/qwen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


def _row(cells: Iterable[Any]) -> str:
    return '<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'


def _table(headers: Iterable[str],
           rows: Iterable[Iterable[Any]]) -> str:
    head = '<tr>' + ''.join(f'<th>{h}</th>' for h in headers) + '</tr>'
    body = ''.join(_row(r) for r in rows)
    return f'<table border="1" cellspacing="0" cellpadding="6">{head}{body}</table>'


def render_executive_html(report: Dict[str, Any]) -> str:
    """report keys: week_label, headline, lift, approvals, returns_top,
    inventory_alerts, drift, cost."""
    week = report.get('week_label', '本周')
    headline = report.get('headline', '')
    lift = report.get('lift') or {}
    approvals = report.get('approvals') or {}
    returns_top = report.get('returns_top') or []
    inventory_alerts = report.get('inventory_alerts') or []
    drift = report.get('drift') or {}
    cost = report.get('cost') or {}

    parts: List[str] = [
        '<!doctype html><html><head><meta charset="utf-8">',
        f'<title>CRO 周报 {week}</title>',
        '<style>body{font-family:Arial,Helvetica,sans-serif;}',
        'h1{color:#222;}h2{color:#444;border-bottom:1px solid #ccc;}',
        'table{border-collapse:collapse;margin:8px 0;}'
        'th{background:#f0f0f0;}</style></head><body>',
        f'<h1>CRO 周报 — {week}</h1>',
        f'<p><b>核心摘要:</b> {headline}</p>',
        '<h2>① A/B Lift</h2>',
        _table(['指标', '值'], [
            ['Treatment lift_pct', lift.get('lift_pct', 'N/A')],
            ['Sample per arm', lift.get('sample_per_arm', 'N/A')],
            ['Significant', lift.get('significant', 'N/A')],
        ]),
        '<h2>② 审批吞吐</h2>',
        _table(['kind', 'pending', 'auto_approve'], [
            [k, v.get('pending', 0), v.get('auto_approve', 0)]
            for k, v in (approvals.get('by_kind') or {}).items()
        ] or [['—', 0, 0]]),
        '<h2>③ 退货 Top</h2>',
        _table(['SKU', 'returns_pct', 'top_reason'], [
            [r.get('sku'), r.get('returns_pct'), r.get('top_reason', '')]
            for r in returns_top[:10]
        ] or [['—', '—', '—']]),
        '<h2>④ 库存告警</h2>',
        _table(['SKU', 'stock', 'days_to_stockout'], [
            [r.get('sku'), r.get('stock'), r.get('days_to_stockout')]
            for r in inventory_alerts[:10]
        ] or [['—', '—', '—']]),
        '<h2>⑤ Cohort drift</h2>',
        f'<p>drift_detected = <b>{drift.get("drift_detected", False)}</b>; '
        f'drift_weeks = {drift.get("drift_weeks", [])}</p>',
        '<h2>⑥ 成本</h2>',
        _table(['指标', '值'], [
            ['今日成本 (USD)', cost.get('today_cost_usd', 0)],
            ['每决策均价', cost.get('avg_cost_per_decision', 0)],
            ['建议降级到规则', cost.get('suggest_downgrade', False)],
        ]),
        f'<p><i>生成时间: {datetime.now(timezone.utc).isoformat()}</i></p>',
        '</body></html>',
    ]
    return ''.join(parts)


def write_html_report(report: Dict[str, Any], path: Path) -> Path:
    html = render_executive_html(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding='utf-8')
    return path


def build_pdf_with_reportlab(report: Dict[str, Any],
                             path: Path) -> Path:
    """可选: 仅在 reportlab 已装时使用. 失败抛 ImportError 给上游."""
    try:
        from reportlab.lib.pagesizes import letter  # type: ignore
        from reportlab.pdfgen import canvas  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError('reportlab not installed') from e

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont('Helvetica-Bold', 16)
    c.drawString(72, 720, f'CRO Weekly — {report.get("week_label", "")}')
    c.setFont('Helvetica', 10)
    c.drawString(72, 700, str(report.get('headline', '')))
    c.showPage()
    c.save()
    return path
