"""S109 — 内部解耦事件总线 (同步).

订阅/发布; 单 handler 异常被吞掉记录, 不阻断其它 handler.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

Handler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._errors: List[Dict[str, Any]] = []

    def subscribe(self, event_name: str, handler: Handler) -> None:
        if not callable(handler):
            raise TypeError('handler must be callable')
        self._handlers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: Handler) -> bool:
        try:
            self._handlers[event_name].remove(handler)
            return True
        except (KeyError, ValueError):
            return False

    def publish(self, event_name: str, payload: Any = None) -> Dict[str, Any]:
        handlers = list(self._handlers.get(event_name, []))
        ok = 0
        failed = 0
        for h in handlers:
            try:
                h(payload)
                ok += 1
            except Exception as e:                # 吞掉, 单 handler 不影响其它
                failed += 1
                err = {'event': event_name, 'handler': repr(h),
                       'error': repr(e)}
                self._errors.append(err)
                logger.warning('EventBus handler failed: %s', err)
        return {'event': event_name, 'fired': len(handlers),
                'ok': ok, 'failed': failed}

    def event_names(self) -> List[str]:
        return [name for name, hs in self._handlers.items() if hs]

    def handler_count(self, event_name: str) -> int:
        return len(self._handlers.get(event_name, []))

    def clear(self) -> None:
        self._handlers.clear()
        self._errors.clear()

    @property
    def errors(self) -> List[Dict[str, Any]]:
        return list(self._errors)
