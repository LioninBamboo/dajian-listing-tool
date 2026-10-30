"""S126 — 统一 Dashboard 数据采集器.

5 个支柱: traffic_light(S50) / baseline(S95) / lqi(S97) / arb(S118) / returns(S111).
所有 fetcher 注入, 异常吞掉返回 None, available_count 反映健康度.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

PILLARS = ('traffic_light', 'baseline', 'lqi', 'arb', 'returns')


def _safe(fn):
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        logger.warning('dashboard fetcher failed', exc_info=True)
        return None


def collect_unified(
    *,
    traffic_light_fetcher: Optional[Callable[[], Any]] = None,
    baseline_fetcher: Optional[Callable[[], Any]] = None,
    lqi_fetcher: Optional[Callable[[], Any]] = None,
    arb_fetcher: Optional[Callable[[], Any]] = None,
    returns_fetcher: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    pillars = {
        'traffic_light': _safe(traffic_light_fetcher),
        'baseline': _safe(baseline_fetcher),
        'lqi': _safe(lqi_fetcher),
        'arb': _safe(arb_fetcher),
        'returns': _safe(returns_fetcher),
    }
    available = [k for k, v in pillars.items() if v is not None]
    missing = [k for k in PILLARS if k not in available]
    return {
        'pillars': pillars,
        'available_count': len(available),
        'available': available,
        'missing': missing,
        'health_ratio': round(len(available) / len(PILLARS), 3),
    }
