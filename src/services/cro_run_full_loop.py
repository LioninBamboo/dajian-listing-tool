"""S123 — 顶层 full-loop 编排服务 (scripts/cro_run_full_loop.py 的核心).

模式: dry_run / apply / email
步骤: build_pipeline → router.execute_batch → 汇总报告 → 可选邮件
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def render_run_report(pipeline_out: Dict[str, Any],
                       executor_out: Dict[str, Any]) -> str:
    lines = ['=== CRO Full Loop Report ===',
             f"Total actions enqueued: {pipeline_out['count']}",
             f"  by_action: {pipeline_out.get('by_action', {})}",
             f"  skipped_blacklisted: {pipeline_out.get('skipped_blacklisted', 0)}",
             f"  skipped_duplicate: {pipeline_out.get('skipped_duplicate', 0)}",
             '',
             '--- Executor ---',
             f"mode: {'dry_run' if executor_out.get('dry_run') else 'apply'}",
             f"ok: {executor_out.get('ok', 0)}",
             f"failed: {executor_out.get('failed', 0)}",
             f"skipped: {executor_out.get('skipped', 0)}",
             ]
    return '\n'.join(lines)


def run_full_loop(
    *,
    build_pipeline_fn: Callable[[], Dict[str, Any]],
    router_execute_batch_fn: Callable[[List[Dict[str, Any]], bool],
                                        Dict[str, Any]],
    mode: str = 'dry_run',
    email_sender: Optional[Callable[[str, str], bool]] = None,
    email_subject: str = 'CRO Full Loop Report',
) -> Dict[str, Any]:
    """编排器: 不直接接 sqlite, 全部依赖注入."""
    if mode not in ('dry_run', 'apply'):
        raise ValueError(f'invalid mode: {mode}')
    dry = mode == 'dry_run'

    try:
        pipeline_out = build_pipeline_fn() or {}
    except Exception as e:
        logger.warning('build_pipeline failed: %s', e)
        pipeline_out = {'rows': [], 'count': 0, 'error': repr(e)}

    rows = pipeline_out.get('rows') or []

    try:
        executor_out = router_execute_batch_fn(rows, dry) or {}
    except Exception as e:
        logger.warning('executor failed: %s', e)
        executor_out = {'results': [], 'count': 0, 'ok': 0, 'failed': 0,
                        'skipped': 0, 'dry_run': dry, 'error': repr(e)}

    report = render_run_report(pipeline_out, executor_out)

    email_sent = False
    if email_sender:
        try:
            email_sent = bool(email_sender(email_subject, report))
        except Exception as e:
            logger.warning('email_sender failed: %s', e)
            email_sent = False

    return {
        'mode': mode,
        'pipeline': pipeline_out,
        'executor': executor_out,
        'report': report,
        'email_sent': email_sent,
    }
