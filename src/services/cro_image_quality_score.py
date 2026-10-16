"""S94 — 主图质量评分.

不强依赖 PIL: 接收 image_meta dict (width/height/file_size_kb/has_text/
                                   has_watermark/whitespace_ratio).
若 PIL 可用, analyze_image_bytes 可读 bytes 抽 width/height/avg_white_ratio.
评分 0-100, 各项加权.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

MIN_RECOMMENDED_LONG_EDGE = 1000
MIN_RECOMMENDED_SHORT_EDGE = 800
EBAY_REQUIRED_LONG_EDGE = 500
MIN_FILE_SIZE_KB = 30
MAX_FILE_SIZE_KB = 7000


def score_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """meta keys: width, height, file_size_kb, has_watermark,
                  has_text_overlay, whitespace_ratio."""
    w = int(meta.get('width', 0) or 0)
    h = int(meta.get('height', 0) or 0)
    long_edge = max(w, h)
    short_edge = min(w, h) if min(w, h) > 0 else 0
    fkb = float(meta.get('file_size_kb', 0) or 0)
    has_wm = bool(meta.get('has_watermark', False))
    has_text = bool(meta.get('has_text_overlay', False))
    ws = float(meta.get('whitespace_ratio', 0) or 0)

    issues = []
    score = 100

    # 分辨率
    if long_edge < EBAY_REQUIRED_LONG_EDGE:
        score -= 40
        issues.append(f'长边 {long_edge}px < eBay 强制要求 500px')
    elif long_edge < MIN_RECOMMENDED_LONG_EDGE:
        score -= 15
        issues.append(f'长边 {long_edge}px < 推荐 1000px')

    if short_edge < MIN_RECOMMENDED_SHORT_EDGE and short_edge > 0:
        score -= 5
        issues.append(f'短边 {short_edge}px < 推荐 800px')

    # 文件大小
    if fkb > 0 and fkb < MIN_FILE_SIZE_KB:
        score -= 10
        issues.append(f'文件 {fkb}KB 偏小, 可能压缩过度')
    elif fkb > MAX_FILE_SIZE_KB:
        score -= 5
        issues.append(f'文件 {fkb}KB 过大, 加载慢')

    # 水印/文字
    if has_wm:
        score -= 30
        issues.append('含水印, eBay 主图政策违规风险')
    if has_text:
        score -= 15
        issues.append('含文字叠加, 主图建议纯产品')

    # 白底比例
    if 0 < ws < 0.30:
        score -= 10
        issues.append(f'白底比例 {ws:.0%} 偏低, 主图建议 ≥30% 白底')
    elif ws > 0.85:
        score -= 5
        issues.append(f'白底比例 {ws:.0%} 过高, 产品占比小')

    score = max(0, min(100, score))
    grade = grade_for(score)
    return {
        'score': score,
        'grade': grade,
        'issues': issues,
        'long_edge': long_edge,
        'short_edge': short_edge,
        'file_size_kb': fkb,
        'whitespace_ratio': ws,
    }


def grade_for(score: int) -> str:
    if score >= 85:
        return 'A'
    if score >= 70:
        return 'B'
    if score >= 50:
        return 'C'
    return 'D'


def analyze_image_bytes(blob: bytes) -> Optional[Dict[str, Any]]:
    """可选: 调用 PIL 读 width/height. 没装 PIL 返 None."""
    try:
        from io import BytesIO
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        img = Image.open(BytesIO(blob))
        return {
            'width': img.width,
            'height': img.height,
            'file_size_kb': round(len(blob) / 1024, 2),
        }
    except Exception:
        return None


def batch_score(metas: list) -> Dict[str, Any]:
    items = [score_meta(m) for m in metas]
    if not items:
        return {'items': [], 'avg_score': 0, 'min_grade': None}
    avg = sum(it['score'] for it in items) / len(items)
    grades = [it['grade'] for it in items]
    order = {'D': 3, 'C': 2, 'B': 1, 'A': 0}
    min_g = max(grades, key=lambda g: order.get(g, 0))
    return {'items': items,
            'avg_score': round(avg, 2),
            'min_grade': min_g,
            'count': len(items)}
