"""S132 — Ops Runbook 自动化故障恢复.

定义命名 runbook (steps=[{name, fn}]); execute_runbook 按序执行,
每步 try/except + 是否 critical (critical 失败终止链, 非 critical 仅记录).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

Step = Dict[str, Any]   # {name, fn, critical?, args?, kwargs?}


class RunbookRegistry:
    def __init__(self):
        self._book: Dict[str, List[Step]] = {}

    def register(self, name: str, steps: List[Step]) -> None:
        if not name:
            raise ValueError('runbook name required')
        if not steps:
            raise ValueError('steps required')
        for s in steps:
            if 'name' not in s or 'fn' not in s:
                raise ValueError('step requires name+fn')
            if not callable(s['fn']):
                raise TypeError(f'step {s["name"]} fn must callable')
        self._book[name] = list(steps)

    def names(self) -> List[str]:
        return sorted(self._book.keys())

    def get(self, name: str) -> List[Step]:
        if name not in self._book:
            raise KeyError(f'unknown runbook: {name}')
        return self._book[name]


def _utcnow() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def execute_runbook(steps: List[Step],
                     *, log_path: Optional[str] = None,
                     dry_run: bool = False) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    aborted = False
    abort_reason: Optional[str] = None
    for s in steps:
        name = s['name']
        critical = bool(s.get('critical', True))
        if dry_run:
            results.append({'name': name, 'status': 'dry_run',
                             'critical': critical})
            continue
        if aborted:
            results.append({'name': name, 'status': 'skipped_after_abort',
                             'critical': critical})
            continue
        try:
            ret = s['fn'](*s.get('args', ()), **s.get('kwargs', {}))
            results.append({'name': name, 'status': 'ok',
                             'critical': critical,
                             'return': ret if _is_jsonable(ret) else str(ret)})
        except Exception as e:
            entry = {'name': name, 'status': 'failed',
                      'critical': critical, 'error': repr(e)}
            results.append(entry)
            if critical:
                aborted = True
                abort_reason = name

    summary = {'results': results,
                'count': len(results),
                'ok': sum(1 for r in results if r['status'] == 'ok'),
                'failed': sum(1 for r in results if r['status'] == 'failed'),
                'aborted': aborted,
                'abort_reason': abort_reason,
                'dry_run': dry_run}

    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path) or '.', exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'ts': _utcnow(), **summary},
                                    ensure_ascii=False, default=str) + '\n')
        except Exception:
            logger.warning('runbook log append failed', exc_info=True)
    return summary


def _is_jsonable(v: Any) -> bool:
    try:
        json.dumps(v, default=str)
        return True
    except Exception:
        return False
