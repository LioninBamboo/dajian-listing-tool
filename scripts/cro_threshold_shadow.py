"""S20 — CRO 阈值 shadow run / A-B 守门员.

每次 cro_thresholds.learn_all 学到新阈值前, 先做一次 shadow:
  1. 用旧阈值 (load_thresholds 当前值) 跑 diagnose_batch
  2. 用新阈值 (proposed) 跑 diagnose_batch
  3. 对比每个品类 P1 入队数 / 健康占比 / 平均分变化
  4. 若任一品类 P1 暴涨 (≥ 当前 1.5x 且绝对增量 ≥ 20) → 阻止 promote, 写报告等人工

调用入口:
  from scripts.cro_threshold_shadow import shadow_compare
  rep = shadow_compare(products, market_data, old_thresholds, new_thresholds)
  if rep['safe_to_promote']:
      # 写库
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPLOSION_RATIO = 1.5
EXPLOSION_ABS = 20


def _diagnose_with(products, market_data, thresholds_by_category):
    from src.services.conversion_diagnoser import diagnose_batch, top_actions
    diags = diagnose_batch(products, market_data=market_data,
                           thresholds_by_category=thresholds_by_category)
    p1 = [a for a in top_actions(diags, limit=10000)
          if a.get('priority') == 1]
    by_cat: Dict[str, Dict[str, int]] = {}
    for d in diags:
        cat = '_unknown'
        # 来自 product 的 categoryId 不在 diagnosis 里, fallback _global
        bucket = by_cat.setdefault(cat, {'total': 0, 'healthy': 0,
                                         'low_ctr': 0, 'low_cvr': 0,
                                         'no_imp': 0, 'p1': 0})
        bucket['total'] += 1
        if d.funnel_stage == 'healthy':
            bucket['healthy'] += 1
        elif d.funnel_stage == 'low_ctr':
            bucket['low_ctr'] += 1
        elif d.funnel_stage == 'low_cvr':
            bucket['low_cvr'] += 1
        elif d.funnel_stage in ('no_impression', 'insufficient_data'):
            bucket['no_imp'] += 1
    p1_by_cat: Dict[str, int] = {}
    for a in p1:
        # action dict 里没 categoryId — 由 product 反查
        sku = a.get('sku')
        cat = next((p.get('categoryId', '_unknown') for p in products
                    if p.get('sku') == sku), '_unknown')
        p1_by_cat[cat] = p1_by_cat.get(cat, 0) + 1
    return {
        'p1_count': len(p1),
        'p1_by_category': p1_by_cat,
        'avg_score': (sum(d.cro_score for d in diags) / len(diags))
                     if diags else 0.0,
        'healthy_count': sum(1 for d in diags if d.funnel_stage == 'healthy'),
        'total': len(diags),
    }


def shadow_compare(products: List[Dict[str, Any]],
                   market_data: Optional[Dict[str, Any]],
                   old_thresholds: Dict[str, Dict[str, float]],
                   new_thresholds: Dict[str, Dict[str, float]],
                   ) -> Dict[str, Any]:
    old = _diagnose_with(products, market_data, old_thresholds or None)
    new = _diagnose_with(products, market_data, new_thresholds or None)

    # 检查每个品类 P1 是否暴涨
    explosions = []
    for cat, new_n in (new['p1_by_category'] or {}).items():
        old_n = (old['p1_by_category'] or {}).get(cat, 0)
        if old_n == 0 and new_n >= EXPLOSION_ABS:
            explosions.append({'category': cat, 'old': 0, 'new': new_n,
                               'ratio': float('inf')})
            continue
        if old_n > 0:
            ratio = new_n / old_n
            if ratio >= EXPLOSION_RATIO and (new_n - old_n) >= EXPLOSION_ABS:
                explosions.append({'category': cat, 'old': old_n,
                                   'new': new_n, 'ratio': round(ratio, 2)})

    return {
        'old': old,
        'new': new,
        'p1_delta': new['p1_count'] - old['p1_count'],
        'avg_score_delta': round(new['avg_score'] - old['avg_score'], 2),
        'explosions': explosions,
        'safe_to_promote': not explosions,
    }


def _write_report(rep: Dict[str, Any]) -> Path:
    out = (PROJECT_ROOT / 'reports'
           / f"cro_threshold_shadow_{datetime.now():%Y%m%d_%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    return out


def main():
    """CLI: shadow vs current — 用 daily_runner 的产品输入跑 shadow.

    实际场景: 每周日学完阈值后, scheduler 调本脚本; 通过则 promote, 否则保留旧值.
    """
    p = argparse.ArgumentParser(description='CRO threshold shadow run')
    p.add_argument('--promote', action='store_true',
                   help='If safe, copy proposed → cro_thresholds; else abort')
    args = p.parse_args()

    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.services.cro_thresholds import load_thresholds, load_pending_thresholds, promote_pending
    from src.web.pages.competition_monitor import (
        load_products_from_db, load_performance_data,
        merge_performance_into_products, get_market_data_from_report,
    )

    old_thr = load_thresholds()
    new_thr = load_pending_thresholds()

    if not new_thr:
        print(json.dumps({'note': 'No pending thresholds to compare. Aborting.'}))
        return

    products = load_products_from_db() or []
    perf = load_performance_data(force_refresh=False)
    merge_performance_into_products(products, perf or {})
    for prod in products:
        prod['images'] = [prod['image_url']] if prod.get('image_url') else []
    market_data = get_market_data_from_report(None)

    rep = shadow_compare(products, market_data or {}, old_thr, new_thr)
    out_path = _write_report(rep)
    
    output = {
        'old_thr_categories': len(old_thr),
        'new_thr_categories': len(new_thr),
        'shadow_result': rep,
        'report_saved_to': str(out_path),
        'promoted': False
    }

    if args.promote:
        if rep.get('safe_to_promote'):
            promoted_count = promote_pending()
            output['promoted'] = True
            output['promoted_categories'] = promoted_count
        else:
            output['note'] = 'Promotion blocked due to safety checks (explosions detected).'

    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
