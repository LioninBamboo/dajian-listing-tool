"""CRO automatic action execution helpers."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from src.services.cro_email_digest import render_auto_execution_email
from src.services.cro_action_queue import (
    enqueue_unique_pending, queue_stats, recent_terminal_keys,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SAFE_AUTO_ACTIONS = ('price_drop', 'image_refresh', 'fill_specifics', 'promote')
DEFAULT_LIMITS = {
    'price_drop': 200,
    'image_refresh': 80,
    'fill_specifics': 80,
    'promote': 200,
}

Executor = Callable[..., Dict[str, Any]]


def _summary_from_report(action_type: str, report: Dict[str, Any]) -> Dict[str, Any]:
    if action_type == 'price_drop':
        results = report.get('results') or []
        return {
            'action': action_type,
            'pending_total': report.get('total_products', len(results)),
            'done': sum(1 for row in results if row.get('status') == 'updated'),
            'failed': sum(1 for row in results if row.get('status') == 'error'),
            'skipped': sum(1 for row in results if row.get('status') not in {'updated', 'error'}),
            'report': report,
        }
    return {
        'action': action_type,
        'pending_total': int(report.get('pending_total', 0) or 0),
        'done': len(report.get('done') or []),
        'failed': len(report.get('failed') or []),
        'skipped': len(report.get('skipped') or []),
        'marked_done': int(report.get('marked_done', 0) or 0),
        'marked_skipped': int(report.get('marked_skipped', 0) or 0),
        'report': report,
    }


def _default_executor(action_type: str) -> Executor:
    if action_type == 'price_drop':
        from scripts.batch_smart_reprice import run_batch_reprice

        def run_price_drop(*, apply_changes: bool, limit: int,
                           send_email: bool) -> Dict[str, Any]:
            return run_batch_reprice(
                dry_run=not apply_changes,
                send_email=send_email,
                from_cro_queue=True,
            )
        return run_price_drop
    if action_type == 'image_refresh':
        from scripts.cro_image_refresh import run

        return lambda *, apply_changes, limit, send_email: run(apply_changes, limit)
    if action_type == 'fill_specifics':
        from scripts.cro_fill_specifics import run

        return lambda *, apply_changes, limit, send_email: run(apply_changes, limit)
    if action_type == 'promote':
        from scripts.cro_promote import run

        return lambda *, apply_changes, limit, send_email: run(
            apply_changes, limit,
            auto_create_missing_ads=True,
        )
    raise ValueError(f'unsupported action_type: {action_type}')


def execute_pending_actions(
    *,
    action_types: Iterable[str] = SAFE_AUTO_ACTIONS,
    apply_changes: bool = True,
    limits: Optional[Dict[str, int]] = None,
    send_email: bool = False,
    executor_map: Optional[Dict[str, Executor]] = None,
) -> Dict[str, Any]:
    """Execute pending queue rows for the safe automatic action set."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    executor_map = executor_map or {}
    summaries: List[Dict[str, Any]] = []
    unsupported: List[str] = []
    for action_type in action_types:
        if action_type not in SAFE_AUTO_ACTIONS:
            unsupported.append(action_type)
            continue
        executor = executor_map.get(action_type) or _default_executor(action_type)
        report = executor(
            apply_changes=apply_changes,
            limit=int(limits.get(action_type, 20) or 20),
            send_email=send_email,
        )
        summaries.append(_summary_from_report(action_type, report or {}))
    result = {
        'apply': apply_changes,
        'actions': summaries,
        'unsupported': unsupported,
        'queue_stats': queue_stats(),
    }
    if send_email and summaries:
        result['email'] = _send_summary_email(result)
    return result


def _send_summary_email(report: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from src.utils.email_sender import send_email
        actions = report.get('actions') or []
        html = render_auto_execution_email(report)
        done = sum(a.get('done', 0) for a in actions)
        failed = sum(a.get('failed', 0) for a in actions)
        send_email(f"CRO 自动执行 · 成功 {done} · 失败 {failed}", html)
        return {'sent': True}
    except Exception as e:
        return {'sent': False, 'error': str(e)}


def auto_enqueue_and_execute(
    actions: Iterable[Dict[str, Any]],
    *,
    source: str = 'cro_ui_auto',
    action_types: Iterable[str] = SAFE_AUTO_ACTIONS,
    enqueue_limit: int = 200,
    priority: int = 1,
    apply_changes: bool = True,
    send_email: bool = False,
    executor_map: Optional[Dict[str, Executor]] = None,
) -> Dict[str, Any]:
    requested = set(action_types)
    allowed = requested.intersection(SAFE_AUTO_ACTIONS)
    action_list = list(actions)
    recently_handled = recent_terminal_keys(hours=24)
    selected = [
        row for row in action_list
        if row.get('priority') == priority and row.get('action') in allowed
        and (str(row.get('sku') or '').strip(), str(row.get('action') or '').strip())
        not in recently_handled
    ][:enqueue_limit]
    skipped_recently_handled = sum(
        1 for row in action_list
        if row.get('priority') == priority and row.get('action') in allowed
        and (str(row.get('sku') or '').strip(), str(row.get('action') or '').strip())
        in recently_handled
    )
    enqueue_result = enqueue_unique_pending(selected, source=source)
    enqueue_result['skipped_recently_handled'] = skipped_recently_handled
    execution = execute_pending_actions(
        action_types=action_types,
        apply_changes=apply_changes,
        send_email=send_email,
        executor_map=executor_map,
    )
    return {
        'selected_count': len(selected),
        'enqueue': enqueue_result,
        'execution': execution,
    }


def write_auto_execution_report(report: Dict[str, Any],
                                report_dir: Optional[Path] = None) -> Path:
    out_dir = Path(report_dir) if report_dir else PROJECT_ROOT / 'logs'
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"cro_auto_execute_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return path