"""S32 \u2014 \u9608\u503c\u53cd\u54fa\u81ea\u52a8 PR.

\u8df1\u8e2a:
  1. \u8dd1 cro_effect_audit.evaluate_actions \u62ff\u5230\u5e26 category \u7684\u6548\u679c\u884c
  2. \u8df1 suggest_threshold_adjustments \u51fa\u5efa\u8bae
  3. apply_suggestions_to_pending \u5199\u5165 cro_thresholds_pending
  4. \u540e\u7eed scripts/cro_promote_thresholds.py \u4f1a\u8dd1 shadow \u4e0a\u751f\u4ea7

\u8c03\u5ea6\u5efa\u8bae: \u6bcf\u5468\u516d 03:00 (early before Sunday\u3000promote_thresholds 02:30).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.services.cro_thresholds import (
    apply_suggestions_to_pending, load_thresholds,
    suggest_threshold_adjustments,
)


def run(write: bool = False, since_days: int = 35,
        window_days: int = 7) -> dict:
    from scripts.cro_effect_audit import evaluate_actions
    audit = evaluate_actions(window_days=window_days, since_days=since_days)
    cur = load_thresholds()
    suggestions = suggest_threshold_adjustments(audit, cur)
    n = 0
    if write and suggestions:
        n = apply_suggestions_to_pending(suggestions)
    return {
        'evaluated': audit['total_evaluated'],
        'suggestions_count': len(suggestions),
        'pending_written': n,
        'suggestions': suggestions,
    }


def main():  # pragma: no cover
    p = argparse.ArgumentParser(description='S32 \u9608\u503c\u53cd\u54fa\u2192pending')
    p.add_argument('--apply', action='store_true',
                   help='\u5199\u5165 cro_thresholds_pending; \u9ed8\u8ba4 dry-run')
    p.add_argument('--since-days', type=int, default=35)
    p.add_argument('--window-days', type=int, default=7)
    p.add_argument('--out', help='write JSON report')
    args = p.parse_args()
    rep = run(write=args.apply, since_days=args.since_days,
              window_days=args.window_days)
    print(f"\u8bc4\u4f30 {rep['evaluated']}\u3000\u5efa\u8bae {rep['suggestions_count']}\u3000"
          f"\u5199\u5165 pending {rep['pending_written']}")
    for s in rep['suggestions'][:10]:
        print(f"  \u00b7 {s.get('category_id')}/{s.get('action')}: "
              f"{s.get('metric')} {s.get('current')} \u2192 {s.get('suggested')} "
              f"({s.get('direction')}, {s.get('improved_rate')*100:.0f}% \u6539\u5584)")
    if args.out:
        Path(args.out).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )


if __name__ == '__main__':  # pragma: no cover
    main()
