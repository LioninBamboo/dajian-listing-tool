"""S43 \u2014 \u884c\u52a8\u53cd\u54fa PricingEngine.

\u4ece CRO \u52a8\u4f5c\u961f\u5217\u4e2d\u5b66\u4e60\u54c1\u7c7b\u5bf9\u4fc3\u9500\u7684\u5f39\u6027:
  - price_drop done \u540e\u9500\u91cf\u5927\u5e45\u63d0\u5347 \u2192 elastic (\u4f4e target_margin)
  - price_drop done \u540e\u9500\u91cf\u51e0\u4e4e\u4e0d\u53d8 \u2192 inelastic (\u9ad8 target_margin)
  - promote_rollback \u9891\u7e41 \u2192 \u4fc3\u9500\u4e0d\u8d5a, \u8be5\u54c1\u7c7b inelastic \u2192 \u9ad8 margin

\u4e0d\u4fee\u6539 PricingEngine, \u63d0\u4f9b\u4e00\u4e2a\u53ef\u88ab adapter \u8c03\u7528\u7684 sidecar:
  recommended_target_margin(category, default=0.15)
\u8c03\u7528\u8005 (\u672a\u6765 batch publish / repricing) \u53ef\u9009\u62e9\u8986\u76d6\u9ed8\u8ba4 0.15.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUEUE = PROJECT_ROOT / 'logs' / 'cro_action_queue.jsonl'

DEFAULT_MARGIN = 0.15
ELASTIC_MARGIN = 0.10   # \u6709\u5f39\u6027 \u2192 \u53ef\u4ee5\u538b\u4ef7
INELASTIC_MARGIN = 0.22  # \u4e0d\u5f39\u4e0d\u52a8 \u2192 \u62a2\u5229\u6da6
MIN_SAMPLES = 5


def _iter_queue(qp: Path) -> Iterable[Dict[str, Any]]:
    if not qp.exists():
        return []
    out = []
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def learn_elasticity(queue_path: Optional[Path] = None,
                     ) -> Dict[str, Dict[str, Any]]:
    """\u8fd4\u56de {category: {samples, elastic_count, inelastic_count, rollback_count, verdict}}."""
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    by_cat: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {'samples': 0, 'elastic': 0, 'inelastic': 0, 'rollback': 0}
    )
    for row in _iter_queue(qp):
        cat = row.get('category') or (row.get('detail') or {}).get('category')
        if not cat:
            continue
        action = row.get('action')
        status = row.get('status')
        if action == 'price_drop' and status == 'done':
            d = row.get('detail') or {}
            lift = d.get('sold_lift_pct')
            if lift is None:
                continue
            by_cat[cat]['samples'] += 1
            if lift >= 0.20:
                by_cat[cat]['elastic'] += 1
            elif lift <= 0.05:
                by_cat[cat]['inelastic'] += 1
        elif action == 'promote' and status == 'done':
            d = row.get('detail') or {}
            if d.get('rolled_back'):
                by_cat[cat]['rollback'] += 1
                by_cat[cat]['samples'] += 1
    out: Dict[str, Dict[str, Any]] = {}
    for cat, stats in by_cat.items():
        verdict = 'unknown'
        if stats['samples'] >= MIN_SAMPLES:
            if stats['elastic'] > stats['inelastic'] + stats['rollback']:
                verdict = 'elastic'
            elif (stats['inelastic'] + stats['rollback']) > stats['elastic']:
                verdict = 'inelastic'
            else:
                verdict = 'mixed'
        out[cat] = {**stats, 'verdict': verdict}
    return out


def recommended_target_margin(category: str,
                              learned: Optional[Dict[str, Dict[str, Any]]] = None,
                              default: float = DEFAULT_MARGIN,
                              queue_path: Optional[Path] = None,
                              ) -> float:
    learned = learned if learned is not None else learn_elasticity(queue_path)
    info = learned.get(category)
    if not info or info['verdict'] == 'unknown':
        return default
    if info['verdict'] == 'elastic':
        return ELASTIC_MARGIN
    if info['verdict'] == 'inelastic':
        return INELASTIC_MARGIN
    return default
