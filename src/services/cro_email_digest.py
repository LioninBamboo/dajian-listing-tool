"""Readable email digests for CRO action execution."""
from __future__ import annotations

import html
import json
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from src.utils.report_images import build_thumbnail_img_html, make_ebay_listing_url


def _escape(value: Any) -> str:
    return html.escape(str(value or ''))


def _parse_image_candidates(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw)
        except Exception:
            items = [raw] if isinstance(raw, str) else []
    out: List[str] = []
    seen = set()
    for item in items:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def load_product_briefs(skus: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    requested = [str(sku).strip() for sku in skus if str(sku).strip()]
    if not requested:
        return {}
    briefs: Dict[str, Dict[str, Any]] = {sku: {'sku': sku} for sku in requested}
    sku_set = set(requested)

    try:
        from src.db.database import SessionLocal
        from src.db.models import Product

        with SessionLocal() as session:
            for product in session.query(Product).filter(Product.sku.in_(sku_set)).all():
                brief = briefs.setdefault(product.sku, {'sku': product.sku})
                title = getattr(product, 'optimized_title', None) or getattr(product, 'dajian_title', None)
                if title:
                    brief['title'] = title
                images = _parse_image_candidates(getattr(product, 'image_urls', None))
                if images and not brief.get('thumbnail_url'):
                    brief['thumbnail_url'] = images[0]
                listing_id = getattr(product, 'ebay_item_id', None)
                if listing_id:
                    brief['listing_id'] = str(listing_id)
                    brief['ebay_url'] = make_ebay_listing_url(str(listing_id))
    except Exception:
        pass

    try:
        from src.db.collection_db import SessionLocal as CollectionSessionLocal
        from src.db.collection_models import CollectedProduct

        with CollectionSessionLocal() as session:
            for product in session.query(CollectedProduct).filter(CollectedProduct.sku.in_(sku_set)).all():
                brief = briefs.setdefault(product.sku, {'sku': product.sku})
                if not brief.get('title') and getattr(product, 'title', None):
                    brief['title'] = product.title
                images = _parse_image_candidates(getattr(product, 'images', None))
                if images and not brief.get('thumbnail_url'):
                    brief['thumbnail_url'] = images[0]
                source_url = getattr(product, 'url', None)
                if source_url:
                    brief['source_url'] = source_url
                listing_id = getattr(product, 'listing_id', None)
                if listing_id and not brief.get('listing_id'):
                    brief['listing_id'] = str(listing_id)
                    brief['ebay_url'] = make_ebay_listing_url(str(listing_id))
    except Exception:
        pass

    for sku, brief in briefs.items():
        brief.setdefault('title', sku)
    return briefs


def summarize_rows_by_sku(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        sku = str(row.get('sku') or '').strip()
        if not sku:
            continue
        item = ordered.setdefault(sku, {
            'sku': sku,
            'occurrences': 0,
            'row': row,
            'status_counts': Counter(),
            'reason_counts': Counter(),
            'actions': Counter(),
        })
        item['occurrences'] += 1
        item['status_counts'][str(row.get('status') or 'unknown')] += 1
        reason = str(row.get('reason') or row.get('message') or '未提供原因').strip()
        item['reason_counts'][reason] += 1
        action = str(row.get('action') or '').strip()
        if action:
            item['actions'][action] += 1
    out: List[Dict[str, Any]] = []
    for sku, item in ordered.items():
        representative = dict(item['row'])
        representative['sku'] = sku
        representative['occurrences'] = item['occurrences']
        representative['status_summary'] = ', '.join(
            f"{status} x{count}" if count > 1 else status
            for status, count in item['status_counts'].most_common()
        )
        representative['reason_summary'] = '；'.join(
            f"{reason} x{count}" if count > 1 else reason
            for reason, count in item['reason_counts'].most_common(2)
        )
        representative['action_summary'] = ', '.join(
            f"{action} x{count}" if count > 1 else action
            for action, count in item['actions'].most_common()
        )
        out.append(representative)
    return out


def _badge(text: str, background: str) -> str:
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:999px;"
        f"background:{background};color:#111;font-size:12px;font-weight:700;'>"
        f"{_escape(text)}</span>"
    )


def _status_badge(status: str) -> str:
    mapping = {
        'done': '#c7f0d8',
        'updated': '#c7f0d8',
        'skipped': '#f6e7b2',
        'failed': '#f8c7c7',
        'error': '#f8c7c7',
        'dry_run': '#d9e8fb',
    }
    return _badge(status or 'unknown', mapping.get(status or '', '#e9ecef'))


def _thumbnail_cell(url: Optional[str]) -> str:
    if url:
        image_html = build_thumbnail_img_html(url, width=72, height=72)
        if image_html:
            return image_html
        return (
            f"<img src='{_escape(url)}' alt='thumb' width='72' height='72' "
            "style='display:block;border-radius:8px;object-fit:cover;border:1px solid #ddd;'>"
        )
    return (
        "<div style='width:72px;height:72px;border-radius:8px;border:1px dashed #bbb;"
        "background:#f7f7f7;color:#888;font-size:12px;line-height:72px;text-align:center;'>"
        "No image</div>"
    )


def _links_cell(brief: Dict[str, Any]) -> str:
    links: List[str] = []
    if brief.get('ebay_url'):
        links.append(f"<a href='{_escape(brief['ebay_url'])}'>eBay</a>")
    if brief.get('source_url'):
        links.append(f"<a href='{_escape(brief['source_url'])}'>Source</a>")
    return ' | '.join(links) if links else '—'


def _detail_lines(row: Dict[str, Any]) -> List[str]:
    details: List[str] = []
    if row.get('action_summary'):
        details.append(f"动作: {_escape(row['action_summary'])}")
    if row.get('created_ad'):
        details.append('结果: 新开 promote 广告')
    if row.get('cur_bid') is not None:
        details.append(f"当前 bid: {_escape(row.get('cur_bid'))}%")
    if row.get('new_bid') is not None:
        details.append(f"目标 bid: {_escape(row.get('new_bid'))}%")
    if row.get('dyn_cap') is not None:
        details.append(f"安全上限: {_escape(row.get('dyn_cap'))}%")
    if row.get('occurrences', 1) > 1:
        details.append(f"队列重复记录: {_escape(row.get('occurrences'))} 条")
    return details


def _render_reason_summary(rows: List[Dict[str, Any]]) -> str:
    reason_counts = Counter(row.get('reason_summary') or row.get('reason') or '未提供原因' for row in rows)
    items = ''.join(
        f"<tr><td style='padding:6px 8px;border:1px solid #eee;'>{_escape(reason)}</td>"
        f"<td style='padding:6px 8px;border:1px solid #eee;text-align:right;'>{count}</td></tr>"
        for reason, count in reason_counts.most_common(8)
    )
    return (
        "<table style='border-collapse:collapse;width:100%;margin:12px 0 18px 0;'>"
        "<tr><th style='text-align:left;padding:6px 8px;border:1px solid #eee;background:#fafafa;'>主要原因</th>"
        "<th style='text-align:right;padding:6px 8px;border:1px solid #eee;background:#fafafa;'>SKU数</th></tr>"
        f"{items}</table>"
    )


def _render_product_table(rows: List[Dict[str, Any]],
                          product_briefs: Dict[str, Dict[str, Any]],
                          *,
                          max_products: int = 30) -> str:
    body = []
    for row in rows[:max_products]:
        brief = product_briefs.get(row['sku'], {'title': row['sku']})
        detail_html = '<br>'.join(_escape(line) for line in _detail_lines(row)) or '—'
        body.append(
            "<tr>"
            f"<td style='padding:10px;border:1px solid #eee;vertical-align:top;width:88px;'>{_thumbnail_cell(brief.get('thumbnail_url'))}</td>"
            f"<td style='padding:10px;border:1px solid #eee;vertical-align:top;'>"
            f"<div style='font-weight:700;margin-bottom:4px;'>{_escape(brief.get('title') or row['sku'])}</div>"
            f"<div style='color:#555;font-size:12px;margin-bottom:6px;'>SKU: {_escape(row['sku'])}</div>"
            f"<div style='font-size:12px;color:#333;'>{detail_html}</div>"
            "</td>"
            f"<td style='padding:10px;border:1px solid #eee;vertical-align:top;width:90px;'>{_status_badge(str(row.get('status') or 'unknown'))}</td>"
            f"<td style='padding:10px;border:1px solid #eee;vertical-align:top;min-width:240px;'>{_escape(row.get('reason_summary') or row.get('reason') or '—')}</td>"
            f"<td style='padding:10px;border:1px solid #eee;vertical-align:top;width:120px;'>{_links_cell(brief)}</td>"
            "</tr>"
        )
    return (
        "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
        "<tr>"
        "<th style='text-align:left;padding:8px;border:1px solid #eee;background:#fafafa;'>缩略图</th>"
        "<th style='text-align:left;padding:8px;border:1px solid #eee;background:#fafafa;'>产品</th>"
        "<th style='text-align:left;padding:8px;border:1px solid #eee;background:#fafafa;'>结果</th>"
        "<th style='text-align:left;padding:8px;border:1px solid #eee;background:#fafafa;'>原因</th>"
        "<th style='text-align:left;padding:8px;border:1px solid #eee;background:#fafafa;'>链接</th>"
        "</tr>"
        + ''.join(body)
        + "</table>"
    )


def render_promote_email(report: Dict[str, Any],
                         *,
                         product_briefs: Optional[Dict[str, Dict[str, Any]]] = None,
                         max_products: int = 30) -> str:
    rows = summarize_rows_by_sku(report.get('rows') or [])
    briefs = product_briefs or load_product_briefs(row['sku'] for row in rows)
    unique_count = len(rows)
    queue_rows = len(report.get('rows') or [])
    all_at_cap = bool(rows) and all('already at cap' in str(row.get('reason_summary') or row.get('reason') or '').lower() for row in rows)
    note = (
        "<p style='margin:10px 0 18px 0;color:#444;'>"
        "本次 promote 已完整执行检查。当前没有继续加价的商品，因为这些商品已在系统允许的安全广告上限。"
        "</p>"
        if all_at_cap else ""
    )
    duplicate_note = (
        f"<p style='margin:8px 0 18px 0;color:#8a6d3b;'>检测到队列记录 {queue_rows} 条，但实际唯一 SKU 为 {unique_count} 个；邮件已按 SKU 合并显示。</p>"
        if queue_rows > unique_count else ""
    )
    return f"""
    <div style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#1f1f1f;max-width:1080px;">
      <h2 style="margin-bottom:8px;">CRO Promote 执行结果</h2>
      <p style="margin:0 0 12px 0;">队列记录: <b>{queue_rows}</b> · 唯一 SKU: <b>{unique_count}</b> · 成功: <b>{len(report.get('done') or [])}</b> · 失败: <b>{len(report.get('failed') or [])}</b> · 跳过: <b>{len(report.get('skipped') or [])}</b></p>
      {note}
      {duplicate_note}
      {_render_reason_summary(rows)}
      {_render_product_table(rows, briefs, max_products=max_products)}
    </div>
    """


def render_auto_execution_email(report: Dict[str, Any],
                                *,
                                product_briefs: Optional[Dict[str, Dict[str, Any]]] = None,
                                max_products: int = 24) -> str:
    actions = report.get('actions') or []
    detail_sections: List[str] = []
    detail_skus: List[str] = []
    action_rows_html = ''.join(
        "<tr>"
        f"<td style='padding:6px 8px;border:1px solid #eee;'>{_escape(action.get('action'))}</td>"
        f"<td style='padding:6px 8px;border:1px solid #eee;text-align:right;'>{action.get('pending_total', 0)}</td>"
        f"<td style='padding:6px 8px;border:1px solid #eee;text-align:right;'>{action.get('done', 0)}</td>"
        f"<td style='padding:6px 8px;border:1px solid #eee;text-align:right;'>{action.get('failed', 0)}</td>"
        f"<td style='padding:6px 8px;border:1px solid #eee;text-align:right;'>{action.get('skipped', 0)}</td>"
        "</tr>"
        for action in actions
    )
    for action in actions:
        raw_report = action.get('report') or {}
        raw_rows = raw_report.get('rows') or []
        normalized_rows = []
        for row in raw_rows[:max_products]:
            normalized = dict(row)
            normalized.setdefault('action', action.get('action'))
            normalized_rows.append(normalized)
            if normalized.get('sku'):
                detail_skus.append(str(normalized['sku']))
        if not normalized_rows:
            continue
        detail_rows = summarize_rows_by_sku(normalized_rows)
        detail_sections.append(
            f"<h3 style='margin:22px 0 10px 0;'>{_escape(action.get('action'))}</h3>"
            + _render_product_table(detail_rows, product_briefs or load_product_briefs(detail_skus), max_products=max_products)
        )
    briefs = product_briefs or load_product_briefs(detail_skus)
    if detail_sections and not product_briefs:
        detail_sections = []
        for action in actions:
            raw_rows = (action.get('report') or {}).get('rows') or []
            normalized_rows = []
            for row in raw_rows[:max_products]:
                normalized = dict(row)
                normalized.setdefault('action', action.get('action'))
                normalized_rows.append(normalized)
            if normalized_rows:
                detail_sections.append(
                    f"<h3 style='margin:22px 0 10px 0;'>{_escape(action.get('action'))}</h3>"
                    + _render_product_table(summarize_rows_by_sku(normalized_rows), briefs, max_products=max_products)
                )
    return f"""
    <div style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#1f1f1f;max-width:1080px;">
      <h2 style="margin-bottom:8px;">CRO 自动执行结果</h2>
      <p style="margin:0 0 14px 0;">本邮件汇总本次自动执行的动作结果；如果有商品明细，会按 SKU 合并并显示缩略图。</p>
      <table style='border-collapse:collapse;width:100%;margin-bottom:16px;'>
        <tr>
          <th style='text-align:left;padding:6px 8px;border:1px solid #eee;background:#fafafa;'>动作</th>
          <th style='text-align:right;padding:6px 8px;border:1px solid #eee;background:#fafafa;'>待处理</th>
          <th style='text-align:right;padding:6px 8px;border:1px solid #eee;background:#fafafa;'>成功</th>
          <th style='text-align:right;padding:6px 8px;border:1px solid #eee;background:#fafafa;'>失败</th>
          <th style='text-align:right;padding:6px 8px;border:1px solid #eee;background:#fafafa;'>跳过</th>
        </tr>
        {action_rows_html}
      </table>
      {''.join(detail_sections) or '<p style="color:#666;">本次没有可展示的商品级明细。</p>'}
    </div>
    """