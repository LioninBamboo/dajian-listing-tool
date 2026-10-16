"""S130 — Release Gate.

判断当前是否可以发版: 测试通过 + 不变量 0 违例 + sentinel critical=0.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def evaluate_release(
    *,
    pytest_result: Dict[str, Any],
    invariant_result: Dict[str, Any],
    sentinel_critical_count: int,
    max_failed: int = 0,
    max_violations: int = 0,
    max_critical: int = 0,
) -> Dict[str, Any]:
    blocked: List[str] = []

    failed = int((pytest_result or {}).get('failed', 0) or 0)
    if failed > max_failed:
        blocked.append(f'pytest_failed={failed}>max{max_failed}')

    violations = int((invariant_result or {}).get('failed_count', 0) or 0)
    if violations > max_violations:
        blocked.append(
            f'invariants_violated={violations}>max{max_violations}')

    crit = int(sentinel_critical_count or 0)
    if crit > max_critical:
        blocked.append(f'sentinel_critical={crit}>max{max_critical}')

    green = not blocked
    summary = ('READY TO RELEASE' if green
               else 'BLOCKED: ' + '; '.join(blocked))
    return {
        'green': green,
        'blocked_by': blocked,
        'summary': summary,
        'metrics': {
            'pytest_failed': failed,
            'invariant_violations': violations,
            'sentinel_critical': crit,
        },
    }


def run_release_check(
    *,
    pytest_callable: Callable[[], Dict[str, Any]],
    invariant_callable: Callable[[], Dict[str, Any]],
    sentinel_callable: Callable[[], int],
    **kwargs,
) -> Dict[str, Any]:
    def _safe(fn, default):
        try:
            return fn()
        except Exception as e:
            logger.warning('release gate fn failed: %s', e)
            return default
    p = _safe(pytest_callable, {'failed': 1, 'error': 'pytest_unavailable'})
    i = _safe(invariant_callable,
              {'failed_count': 1, 'error': 'invariants_unavailable'})
    s = _safe(sentinel_callable, 1)
    out = evaluate_release(pytest_result=p, invariant_result=i,
                            sentinel_critical_count=s, **kwargs)
    out['raw'] = {'pytest': p, 'invariants': i, 'sentinel_critical': s}
    return out
