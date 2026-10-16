"""S97 — Listing Quality Index (LQI).

加权 5 维: title / item_specifics / images / price_position / inventory.
每维 0-100, 权重默认 0.25/0.20/0.25/0.15/0.15.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_WEIGHTS = {
    'title': 0.25,
    'item_specifics': 0.20,
    'images': 0.25,
    'price_position': 0.15,
    'inventory': 0.15,
}


def score_title(title: str) -> int:
    if not title:
        return 0
    n = len(title)
    if n < 30:
        return 40
    if n < 60:
        return 70
    if n <= 80:
        return 95
    if n <= 100:
        return 80
    return 60


def score_item_specifics(specs: Dict[str, Any],
                         required_fields: int = 8) -> int:
    if not specs:
        return 0
    filled = sum(1 for v in specs.values() if v not in (None, '', 'N/A'))
    pct = min(1.0, filled / max(1, required_fields))
    return int(round(pct * 100))


def score_images(image_count: int,
                 avg_image_score: Optional[float] = None) -> int:
    if image_count <= 0:
        return 0
    count_part = min(1.0, image_count / 6.0) * 60  # 6+ 张满分
    quality_part = (avg_image_score or 70) * 0.4   # 默认 70
    return int(round(count_part + quality_part))


def score_price_position(position: str) -> int:
    table = {
        'cheap': 70,         # 偏低 → 价值感被低估
        'below_median': 90,
        'above_median': 80,
        'expensive': 50,
        'unknown': 60,
    }
    return table.get(position, 60)


def score_inventory(stock: int,
                    days_runway: Optional[float] = None) -> int:
    if stock <= 0:
        return 0
    if days_runway is None:
        return 70  # 没有销售数据时给中等分
    if days_runway < 3:
        return 30
    if days_runway < 7:
        return 60
    if days_runway < 21:
        return 90
    return 80  # 太多库存 (滞销风险) 也扣分


def compute_lqi(features: Dict[str, Any],
                weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """features keys: title, item_specifics, image_count, avg_image_score,
                       price_position, stock, days_runway."""
    weights = weights or DEFAULT_WEIGHTS
    parts = {
        'title': score_title(features.get('title', '')),
        'item_specifics': score_item_specifics(
            features.get('item_specifics') or {},
            features.get('required_specifics_count', 8)),
        'images': score_images(int(features.get('image_count', 0) or 0),
                                features.get('avg_image_score')),
        'price_position': score_price_position(
            features.get('price_position', 'unknown')),
        'inventory': score_inventory(int(features.get('stock', 0) or 0),
                                      features.get('days_runway')),
    }
    total = 0.0
    weight_sum = 0.0
    for k, v in parts.items():
        w = weights.get(k, 0.0)
        total += v * w
        weight_sum += w
    final = total / weight_sum if weight_sum > 0 else 0.0
    return {
        'lqi': int(round(final)),
        'parts': parts,
        'weights': dict(weights),
        'grade': grade_for(final),
    }


def grade_for(score: float) -> str:
    if score >= 85:
        return 'A'
    if score >= 70:
        return 'B'
    if score >= 50:
        return 'C'
    return 'D'


def find_weakest_dimension(parts: Dict[str, int]) -> str:
    if not parts:
        return ''
    return min(parts, key=parts.get)
