"""每日 CRO 任务: 跑 diagnose_batch → 落 cro_snapshots → diff 昨日 → 自动入队 P1.

设计:
  - 数据源: load_products_from_db + merge_performance_into_products (复用 competition_monitor)
  - 不直接调外部 API
  - 输出 reports/cro_daily_<date>.json + 写 cro_action_queue.jsonl
  - 由 daily_tasks.py / scheduler_daemon 在 09:30 调度
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.services.conversion_diagnoser import (
    diagnose_batch, summarize, top_actions,
)
from src.services.cro_snapshot_store import (
    save_snapshots, diff_snapshots, yesterday_iso,
)
from src.services.cro_action_queue import (
    enqueue_unique_pending, queue_stats, recent_terminal_keys,
)
from src.services.cro_thresholds import load_thresholds
from src.services.cro_inventory_filter import filter_safe_actions
from src.services.cro_diagnose_blacklist import filter_blacklisted

REPORT_DIR = Path(__file__).resolve().parents[2] / "reports"


def run_cro_daily(products: List[Dict[str, Any]],
                  market_data: Optional[Dict[str, Any]] = None,
                  enqueue_p1: bool = True,
                  enqueue_action_types: tuple = ('price_drop',),
                  enqueue_limit: int = 30,
                  snapshot_date: Optional[str] = None,
                  report_dir: Optional[Path] = None) -> Dict[str, Any]:
    """跑一次 daily CRO. 返回 report dict (并落盘).

    enqueue_action_types: 默认只把 price_drop 自动排队 (其它建议人工审核).
    """
    sd = snapshot_date or date.today().isoformat()
    learned_thresholds = load_thresholds()
    products, blacklisted = filter_blacklisted(products)
    diagnoses = diagnose_batch(
        products,
        market_data=market_data or {},
        thresholds_by_category=learned_thresholds or None,
    )
    summary = summarize(diagnoses)
    summary['learned_categories'] = len(learned_thresholds)
    summary['blacklisted_skipped'] = len(blacklisted)

    saved = save_snapshots(diagnoses, snapshot_date=sd)
    delta = diff_snapshots(sd, yesterday_iso())

    queued = 0
    inv_dropped = 0
    recently_handled_skipped = 0
    duplicate_skipped = 0
    if enqueue_p1:
        all_actions: List[Dict[str, Any]] = []
        for at in enqueue_action_types:
            all_actions.extend(top_actions(diagnoses, limit=enqueue_limit, action_type=at))
        # 仅 P1
        p1_actions = [a for a in all_actions if a.get('priority') == 1]
        # S27: 库存联动过滤 (price_drop / promote)
        if p1_actions:
            filt = filter_safe_actions(p1_actions)
            inv_dropped = len(filt['dropped'])
            p1_actions = filt['kept']
        if p1_actions:
            recent_keys = recent_terminal_keys(hours=24)
            fresh_actions = []
            for action in p1_actions:
                key = (str(action.get('sku') or '').strip(), str(action.get('action') or '').strip())
                if key in recent_keys:
                    recently_handled_skipped += 1
                    continue
                fresh_actions.append(action)
            enqueue_result = enqueue_unique_pending(fresh_actions, source='cro_daily') if fresh_actions else {
                'added': 0,
                'skipped_duplicate': 0,
                'input_count': 0,
            }
            queued = enqueue_result['added']
            duplicate_skipped = enqueue_result['skipped_duplicate']

    report = {
        'date': sd,
        'summary': summary,
        'delta_vs_yesterday': delta,
        'snapshots_saved': saved,
        'p1_queued': queued,
        'p1_inventory_dropped': inv_dropped,
        'p1_recently_handled_skipped': recently_handled_skipped,
        'p1_duplicate_skipped': duplicate_skipped,
        'queue_stats': queue_stats(),
    }

    rd = Path(report_dir) if report_dir else REPORT_DIR
    rd.mkdir(parents=True, exist_ok=True)
    out = rd / f"cro_daily_{sd}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report['report_path'] = str(out)
    return report
