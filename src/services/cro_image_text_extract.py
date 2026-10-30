"""S112 — 主图文字密度评估.

接收 OCR 已提取结果 (text_boxes=[{text,bbox:[x,y,w,h],confidence}]),
不依赖任何 OCR 引擎. 计算文字面积占比 + 命中违规词.
eBay/Amazon 主图文字超 20% 普遍降权.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

# eBay 不允许的覆盖文字 (常见促销词)
PROHIBITED_WORDS = {
    'sale', 'discount', '%', 'off', 'free', 'shipping', 'limited',
    'best', 'deal', 'cheap', 'guarantee', 'warranty',
    '折扣', '促销', '免费', '包邮', '限时', '保证',
}

# 阈值
HIGH_TEXT_AREA_RATIO = 0.20    # >=20% 算高
MEDIUM_TEXT_AREA_RATIO = 0.10  # 10-20% 中
MIN_CONFIDENCE = 0.50          # 低于此置信度的 box 忽略


def _box_area(box: Dict[str, Any]) -> float:
    bbox = box.get('bbox') or []
    if len(bbox) < 4:
        return 0.0
    w = max(0.0, float(bbox[2] or 0))
    h = max(0.0, float(bbox[3] or 0))
    return w * h


def text_area_ratio(text_boxes: Iterable[Dict[str, Any]],
                    image_width: float,
                    image_height: float) -> float:
    if image_width <= 0 or image_height <= 0:
        return 0.0
    total = image_width * image_height
    text_area = 0.0
    for b in text_boxes:
        if float(b.get('confidence', 1.0) or 0) < MIN_CONFIDENCE:
            continue
        text_area += _box_area(b)
    return min(1.0, text_area / total)


def find_prohibited(text_boxes: Iterable[Dict[str, Any]]) -> List[str]:
    hits: List[str] = []
    for b in text_boxes:
        if float(b.get('confidence', 1.0) or 0) < MIN_CONFIDENCE:
            continue
        text = (b.get('text') or '').lower()
        if not text:
            continue
        for w in PROHIBITED_WORDS:
            if w in text and w not in hits:
                hits.append(w)
    return hits


def evaluate_main_image(text_boxes: Iterable[Dict[str, Any]],
                         image_width: float,
                         image_height: float) -> Dict[str, Any]:
    boxes = list(text_boxes or [])
    ratio = text_area_ratio(boxes, image_width, image_height)
    prohibited = find_prohibited(boxes)
    if ratio >= HIGH_TEXT_AREA_RATIO or prohibited:
        verdict = 'reject'
        reason_parts = []
        if ratio >= HIGH_TEXT_AREA_RATIO:
            reason_parts.append(f'文字占 {ratio:.1%} 超阈')
        if prohibited:
            reason_parts.append(f'命中违规词 {prohibited}')
        reason = '; '.join(reason_parts)
    elif ratio >= MEDIUM_TEXT_AREA_RATIO:
        verdict = 'review'
        reason = f'文字占 {ratio:.1%} 偏高'
    else:
        verdict = 'pass'
        reason = f'文字占 {ratio:.1%} 合规'
    return {
        'text_area_ratio': round(ratio, 4),
        'box_count': len(boxes),
        'prohibited_hits': prohibited,
        'verdict': verdict,
        'reason': reason,
    }


def batch_evaluate(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """rows: [{sku, image_url, width, height, text_boxes:[...]}]"""
    items = []
    rejects = 0
    for r in rows:
        out = evaluate_main_image(r.get('text_boxes') or [],
                                   float(r.get('width', 0) or 0),
                                   float(r.get('height', 0) or 0))
        out['sku'] = r.get('sku')
        out['image_url'] = r.get('image_url')
        items.append(out)
        if out['verdict'] == 'reject':
            rejects += 1
    return {'items': items, 'count': len(items), 'reject_count': rejects}
