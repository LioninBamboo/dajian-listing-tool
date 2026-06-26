"""CRO 行动队列 — 把 UI/调度产生的 P1 改价建议落到 jsonl,
被 batch_smart_reprice / title_optimizer 等执行器消费.

只负责"写指令", 不直接调 eBay; 由现有发布/改价管道做最终守门 (PricingEngine + RepricingGuard).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.services.cro_ab import assign_cohort

DEFAULT_QUEUE = Path(__file__).resolve().parents[2] / "logs" / "cro_action_queue.jsonl"


def enqueue(actions: Iterable[Dict[str, Any]],
            queue_path: Optional[Path] = None,
            source: str = 'cro_diagnoser') -> int:
    """把 action dict 列表追加到队列. 每条带 enqueued_at + source.

    action dict 形如 top_actions() 输出: {sku, listing_id, action, priority, reason, expected_lift, detail, ...}
    """
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    qp.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    n = 0
    with qp.open('a', encoding='utf-8') as f:
        for a in actions:
            row = dict(a)
            row['enqueued_at'] = ts
            row['source'] = source
            row['status'] = 'pending'
            sku = row.get('sku')
            act = row.get('action')
            if sku and act and 'cohort' not in row:
                row['cohort'] = assign_cohort(str(sku), str(act))
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            n += 1
    return n


def _pending_keys(queue_path: Optional[Path] = None) -> set[tuple[str, str]]:
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    if not qp.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sku = str(row.get('sku') or '').strip()
            action = str(row.get('action') or '').strip()
            if row.get('status') == 'pending' and sku and action:
                keys.add((sku, action))
    return keys


def recent_terminal_keys(queue_path: Optional[Path] = None,
                         hours: int = 24) -> set[tuple[str, str]]:
    """Return SKU/action pairs marked done or skipped within the recent window."""
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    if not qp.exists():
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    keys: set[tuple[str, str]] = set()
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('status') not in {'done', 'skipped'}:
                continue
            done_at = row.get('done_at')
            if not done_at:
                continue
            try:
                done_dt = datetime.fromisoformat(str(done_at).replace('Z', '+00:00'))
            except (TypeError, ValueError):
                continue
            if done_dt.tzinfo is None:
                done_dt = done_dt.replace(tzinfo=timezone.utc)
            if done_dt < cutoff:
                continue
            sku = str(row.get('sku') or '').strip()
            action = str(row.get('action') or '').strip()
            if sku and action:
                keys.add((sku, action))
    return keys


def enqueue_unique_pending(actions: Iterable[Dict[str, Any]],
                           queue_path: Optional[Path] = None,
                           source: str = 'cro_diagnoser'
                           ) -> Dict[str, int]:
    """Append actions while skipping rows already pending for the same SKU/action."""
    existing = _pending_keys(queue_path)
    selected: List[Dict[str, Any]] = []
    skipped_duplicate = 0
    for action in actions:
        sku = str(action.get('sku') or '').strip()
        action_type = str(action.get('action') or '').strip()
        if not sku or not action_type:
            continue
        key = (sku, action_type)
        if key in existing:
            skipped_duplicate += 1
            continue
        existing.add(key)
        selected.append(action)
    added = enqueue(selected, queue_path=queue_path, source=source) if selected else 0
    return {
        'added': added,
        'skipped_duplicate': skipped_duplicate,
        'input_count': added + skipped_duplicate,
    }


def load_pending(action_type: Optional[str] = None,
                 queue_path: Optional[Path] = None,
                 include_control: bool = False) -> List[Dict[str, Any]]:
    """默认跳过 cohort='control' 的条目, 让 A/B 对照组不被执行器消费."""
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    if not qp.exists():
        return []
    out = []
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('status') != 'pending':
                continue
            if action_type and row.get('action') != action_type:
                continue
            if not include_control and row.get('cohort') == 'control':
                continue
            out.append(row)
    return out


def mark_done(skus: Iterable[str], action: str,
              queue_path: Optional[Path] = None,
              result: str = 'done') -> int:
    """把队列里匹配 (sku, action) 的 pending 行标记为 result.
    简单实现: 全量重写文件 (队列规模 <几千条, 可接受)."""
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    if not qp.exists():
        return 0
    sku_set = set(skus)
    rows: List[Dict[str, Any]] = []
    n = 0
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row.get('status') == 'pending'
                    and row.get('action') == action
                    and row.get('sku') in sku_set):
                row['status'] = result
                row['done_at'] = datetime.now(timezone.utc).isoformat()
                n += 1
            rows.append(row)
    with qp.open('w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    return n


def queue_stats(queue_path: Optional[Path] = None) -> Dict[str, Any]:
    qp = Path(queue_path) if queue_path else DEFAULT_QUEUE
    if not qp.exists():
        return {
            'total': 0, 'pending': 0, 'pending_executable': 0,
            'pending_control': 0, 'done': 0, 'skipped': 0,
            'by_status': {}, 'by_action': {}, 'pending_by_action': {},
            'pending_executable_by_action': {}, 'done_by_action': {},
            'skipped_by_action': {},
        }
    total = pending = pending_executable = pending_control = done = skipped = 0
    by_status: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    pending_by_action: Dict[str, int] = {}
    pending_executable_by_action: Dict[str, int] = {}
    done_by_action: Dict[str, int] = {}
    skipped_by_action: Dict[str, int] = {}
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            action = str(row.get('action') or 'unknown')
            by_action[action] = by_action.get(action, 0) + 1
            s = str(row.get('status', '') or 'unknown')
            by_status[s] = by_status.get(s, 0) + 1
            if s == 'pending':
                pending += 1
                pending_by_action[action] = pending_by_action.get(action, 0) + 1
                if row.get('cohort') == 'control':
                    pending_control += 1
                else:
                    pending_executable += 1
                    pending_executable_by_action[action] = (
                        pending_executable_by_action.get(action, 0) + 1
                    )
            elif s == 'done':
                done += 1
                done_by_action[action] = done_by_action.get(action, 0) + 1
            elif s == 'skipped':
                skipped += 1
                skipped_by_action[action] = skipped_by_action.get(action, 0) + 1
    return {
        'total': total,
        'pending': pending,
        'pending_executable': pending_executable,
        'pending_control': pending_control,
        'done': done,
        'skipped': skipped,
        'by_status': by_status,
        'by_action': by_action,
        'pending_by_action': pending_by_action,
        'pending_executable_by_action': pending_executable_by_action,
        'done_by_action': done_by_action,
        'skipped_by_action': skipped_by_action,
    }
