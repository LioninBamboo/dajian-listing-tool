"""S122 — Action Executor Router.

按 action_type 路由到具体 executor (注入). 异常吞掉, 写 event_bus.
所有 executor 接口: callable(row: dict) -> dict (status='ok'|'failed' + detail).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

Executor = Callable[[Dict[str, Any]], Dict[str, Any]]


def _default_noop(row: Dict[str, Any]) -> Dict[str, Any]:
    return {'status': 'skipped', 'reason': 'no_executor_registered',
            'action_type': row.get('action_type')}


class ActionRouter:
    def __init__(self,
                  *,
                  event_bus=None,
                  default_executor: Executor = None):
        self._executors: Dict[str, Executor] = {}
        self._event_bus = event_bus
        self._default = default_executor or _default_noop

    def register(self, action_type: str, executor: Executor) -> None:
        if not callable(executor):
            raise TypeError('executor must be callable')
        self._executors[action_type] = executor

    def has_executor(self, action_type: str) -> bool:
        return action_type in self._executors

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if not self._event_bus:
            return
        try:
            self._event_bus.publish(event, payload)
        except Exception:
            logger.warning('event_bus publish failed', exc_info=True)

    def execute_one(self, row: Dict[str, Any]) -> Dict[str, Any]:
        action = row.get('action_type')
        executor = self._executors.get(action, self._default)
        try:
            res = executor(row) or {}
            status = res.get('status', 'ok')
            outcome = {'sku': row.get('sku'),
                        'action_type': action,
                        'status': status,
                        **{k: v for k, v in res.items() if k != 'status'}}
            self._emit('action.executed', outcome)
            return outcome
        except Exception as e:
            outcome = {'sku': row.get('sku'),
                        'action_type': action,
                        'status': 'failed',
                        'error': repr(e)}
            self._emit('action.failed', outcome)
            logger.warning('executor raised: %s', outcome)
            return outcome

    def execute_batch(self, rows: Iterable[Dict[str, Any]],
                       *, dry_run: bool = False) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        ok = failed = skipped = 0
        for row in rows:
            if dry_run:
                results.append({'sku': row.get('sku'),
                                 'action_type': row.get('action_type'),
                                 'status': 'dry_run'})
                continue
            r = self.execute_one(row)
            results.append(r)
            if r['status'] == 'ok':
                ok += 1
            elif r['status'] == 'failed':
                failed += 1
            else:
                skipped += 1
        return {'results': results, 'count': len(results),
                'ok': ok, 'failed': failed, 'skipped': skipped,
                'dry_run': dry_run}
