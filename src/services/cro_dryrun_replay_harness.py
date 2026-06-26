"""S129 — Dry-run / Replay Harness.

记录所有外部调用 → jsonl, 后续 replay 阶段重放为确定性 fetcher.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _key(name: str, args: Tuple, kwargs: Dict[str, Any]) -> str:
    try:
        return json.dumps({'n': name,
                            'a': list(args),
                            'k': kwargs},
                           sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        return f'{name}|{repr(args)}|{repr(sorted(kwargs.items()))}'


class RecordingHarness:
    def __init__(self, record_path: str):
        if not record_path:
            raise ValueError('record_path required')
        self._path = record_path
        self._lock = threading.RLock()

    def _append(self, rec: Dict[str, Any]) -> None:
        rec = {**rec, 'ts': datetime.now(UTC).replace(tzinfo=None).isoformat()}
        try:
            os.makedirs(os.path.dirname(self._path) or '.', exist_ok=True)
            with self._lock:
                with open(self._path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(rec, ensure_ascii=False,
                                        default=str) + '\n')
        except Exception:
            logger.warning('record append failed', exc_info=True)

    def wrap_fetcher(self, name: str, fn: Callable) -> Callable:
        def wrapped(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                self._append({'kind': 'fetcher', 'name': name,
                               'args': list(args), 'kwargs': kwargs,
                               'error': repr(e)})
                raise
            self._append({'kind': 'fetcher', 'name': name,
                           'args': list(args), 'kwargs': kwargs,
                           'result': result})
            return result
        return wrapped

    def wrap_writer(self, name: str, fn: Callable) -> Callable:
        def wrapped(*args, **kwargs):
            self._append({'kind': 'writer', 'name': name,
                           'args': list(args), 'kwargs': kwargs,
                           'side_effect': True})
            return fn(*args, **kwargs)
        return wrapped


def load_events(path: str) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def build_replay_fetcher(events: List[Dict[str, Any]],
                          name: str) -> Callable:
    """根据 (name, args, kwargs) 返回预录制的结果 (顺序匹配)."""
    queue: List[Any] = []
    for e in events:
        if e.get('kind') == 'fetcher' and e.get('name') == name \
                and 'result' in e:
            queue.append({
                'key': _key(name, tuple(e.get('args') or ()),
                             e.get('kwargs') or {}),
                'result': e['result'],
            })

    def fetcher(*args, **kwargs):
        k = _key(name, args, kwargs)
        # 优先精确匹配 + 弹出
        for i, item in enumerate(queue):
            if item['key'] == k:
                return queue.pop(i)['result']
        # 找不到就按顺序兜底, 也弹出
        if queue:
            return queue.pop(0)['result']
        raise LookupError(f'no recorded result for {name}/{k}')
    return fetcher
