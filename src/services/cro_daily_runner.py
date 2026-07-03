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
from src.services.cro_promote_escalation import (
    at_cap_cooldown_skus, escalation_candidates,
)

REPORT_DIR = Path(__file__).resolve().parents[2] / "reports"


def run_cro_daily(products: List[Dict[str, Any]],
                  market_data: Optional[Dict[str, Any]] = None,
                  enqueue_p1: bool = True,
                  enqueue_action_types: tuple = ('price_drop',),
                  enqueue_limit: int = 30,
                  enqueue_max_priority: int = 1,
                  promote_at_cap_cooldown_days: int = 7,
                  snapshot_date: Optional[str] = None,
                  report_dir: Optional[Path] = None) -> Dict[str, Any]:
    """跑一次 daily CRO. 返回 report dict (并落盘).

    enqueue_action_types: 默认只把 price_drop 自动排队 (其它建议人工审核).
    enqueue_max_priority: 允许自动入队的最低优先级 (1=仅 P1; 2=P1+P2).
      诊断器给 image_refresh / fill_specifics 恒为 P2, 这两类要进入
      10:15/10:20 执行器的队列必须由调用方放宽到 2. P3 (delist) 和
      P4 (反向提价) 永远不应自动入队.
    promote_at_cap_cooldown_days: 最近 N 天内被 promote 执行器判定
      'already at cap' 的 SKU 不再重复入队 promote (0 = 关闭冷却),
      名额让给可加价的 SKU; 这些 SKU 以 promote_escalation 形式
      进入日报, 等待非广告手段 (标题重写 / relist).
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
    at_cap_cooldown_dropped = 0
    queued_by_action: Dict[str, int] = {}
    promote_escalation: List[Dict[str, Any]] = []
    if enqueue_p1:
        # 上限 2: delist (P3) 必须人工确认, 反向提价 (P4) 不自动执行
        max_prio = min(int(enqueue_max_priority), 2)
        all_actions: List[Dict[str, Any]] = []
        for at in enqueue_action_types:
            all_actions.extend(top_actions(diagnoses, limit=enqueue_limit, action_type=at))
        auto_actions = [
            a for a in all_actions
            if isinstance(a.get('priority'), int) and a['priority'] <= max_prio
        ]
        # promote at-cap 冷却: 广告杠杆已打满的 SKU 不再占用入队名额
        if promote_at_cap_cooldown_days > 0 and any(
                a.get('action') == 'promote' for a in auto_actions):
            try:
                cooldown = at_cap_cooldown_skus(days=promote_at_cap_cooldown_days)
            except Exception:
                cooldown = set()
            if cooldown:
                kept = []
                for a in auto_actions:
                    if (a.get('action') == 'promote'
                            and str(a.get('sku') or '').strip() in cooldown):
                        at_cap_cooldown_dropped += 1
                        continue
                    kept.append(a)
                auto_actions = kept
        # S27: 库存联动过滤 (price_drop / promote)
        if auto_actions:
            filt = filter_safe_actions(auto_actions)
            inv_dropped = len(filt['dropped'])
            auto_actions = filt['kept']
        if auto_actions:
            recent_keys = recent_terminal_keys(hours=24)
            fresh_by_action: Dict[str, List[Dict[str, Any]]] = {}
            for action in auto_actions:
                sku = str(action.get('sku') or '').strip()
                act = str(action.get('action') or '').strip()
                if (sku, act) in recent_keys:
                    recently_handled_skipped += 1
                    continue
                fresh_by_action.setdefault(act, []).append(action)
            # 按动作类型分批入队, 以便日报按类型统计实际入队量
            for act, fresh_actions in fresh_by_action.items():
                enqueue_result = enqueue_unique_pending(fresh_actions, source='cro_daily')
                queued += enqueue_result['added']
                duplicate_skipped += enqueue_result['skipped_duplicate']
                if enqueue_result['added']:
                    queued_by_action[act] = enqueue_result['added']

    if promote_at_cap_cooldown_days > 0:
        try:
            promote_escalation = escalation_candidates(
                days=promote_at_cap_cooldown_days)
        except Exception:
            promote_escalation = []

    report = {
        'date': sd,
        'summary': summary,
        'delta_vs_yesterday': delta,
        'snapshots_saved': saved,
        'p1_queued': queued,
        'p1_inventory_dropped': inv_dropped,
        'p1_recently_handled_skipped': recently_handled_skipped,
        'p1_duplicate_skipped': duplicate_skipped,
        'queued_by_action': queued_by_action,
        'promote_at_cap_cooldown_dropped': at_cap_cooldown_dropped,
        'promote_escalation': {
            'count': len(promote_escalation),
            'candidates': promote_escalation,
        },
        'queue_stats': queue_stats(),
    }

    rd = Path(report_dir) if report_dir else REPORT_DIR
    rd.mkdir(parents=True, exist_ok=True)
    out = rd / f"cro_daily_{sd}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report['report_path'] = str(out)
    return report
