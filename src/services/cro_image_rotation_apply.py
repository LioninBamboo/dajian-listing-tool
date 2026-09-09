"""S58 — 主图轮换接 eBay revise.

把 S47 schedule_rotation 的输出接到真 eBay revise 调用。
revise_callable(sku, image_url) 由调用方注入避免硬依赖 real_ebay_client。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.services.cro_image_ab_rotation import schedule_rotation


def apply_rotation_for_skus(
    plan: List[Dict[str, Any]],
    *,
    week_now: int,
    revise_callable: Optional[Callable[[str, str], bool]] = None,
    rotation_path: Optional[Path] = None,
    cohort_writer: Optional[Callable[[str, str, int], None]] = None,
) -> Dict[str, Any]:
    """plan: [{sku, candidates:[url,...], last_metrics:{sold,views} 可选}].

    返回 {applied: [{sku, idx, url, revise_ok}], skipped: [...]}.
    revise_callable=None → dry-run, 只算调度结果不调 revise.
    """
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in plan:
        sku = item.get('sku')
        cands = item.get('candidates') or []
        if not sku or len(cands) < 2:
            skipped.append({'sku': sku, 'reason': 'insufficient_candidates'})
            continue
        decision = schedule_rotation(
            sku, cands, week_now=week_now,
            last_week_metrics=item.get('last_metrics'),
            path=rotation_path,
        )
        revise_ok: Optional[bool] = None
        if revise_callable is not None:
            try:
                revise_ok = bool(revise_callable(sku, decision['image_url']))
            except Exception:
                revise_ok = False
        if cohort_writer is not None:
            try:
                cohort_writer(sku, f'image_idx_{decision["idx"]}', week_now)
            except Exception:
                pass
        applied.append({
            'sku': sku,
            'idx': decision['idx'],
            'image_url': decision['image_url'],
            'revise_ok': revise_ok,
        })
    return {
        'applied': applied,
        'skipped': skipped,
        'success_count': sum(1 for a in applied if a['revise_ok'] is True),
        'fail_count': sum(1 for a in applied if a['revise_ok'] is False),
        'dry_run': revise_callable is None,
    }
