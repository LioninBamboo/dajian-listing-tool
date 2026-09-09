"""S88 — 重试队列.

指数退避: delay = base * (factor ** attempt) + jitter.
持久化失败任务到 logs/cro_retry_queue.jsonl, 状态 pending|done|dead.
不依赖 sqlite/eBay; sender 注入.
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_LOG = Path('logs/cro_retry_queue.jsonl')
DEFAULT_BASE_SECONDS = 1.0
DEFAULT_FACTOR = 2.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_DELAY = 300.0   # 5 min cap


def compute_backoff(attempt: int,
                    *,
                    base: float = DEFAULT_BASE_SECONDS,
                    factor: float = DEFAULT_FACTOR,
                    max_delay: float = DEFAULT_MAX_DELAY,
                    jitter_ratio: float = 0.1,
                    rng: Optional[random.Random] = None,
                    ) -> float:
    raw = base * (factor ** max(0, attempt))
    raw = min(raw, max_delay)
    if jitter_ratio > 0:
        r = rng or random
        raw += r.uniform(-raw * jitter_ratio, raw * jitter_ratio)
    return max(0.0, raw)


def with_retry(fn: Callable[[], Any],
               *,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               base: float = DEFAULT_BASE_SECONDS,
               factor: float = DEFAULT_FACTOR,
               max_delay: float = DEFAULT_MAX_DELAY,
               jitter_ratio: float = 0.0,
               sleep: Callable[[float], None] = time.sleep,
               retry_on: tuple = (Exception,),
               ) -> Dict[str, Any]:
    """同步执行带重试. 成功返 {ok, result, attempts}; 失败返 {ok:False,...}."""
    attempts = 0
    last_err: Optional[str] = None
    while attempts < max_attempts:
        attempts += 1
        try:
            result = fn()
            return {'ok': True, 'result': result, 'attempts': attempts,
                    'last_error': None}
        except retry_on as e:
            last_err = repr(e)
            if attempts >= max_attempts:
                break
            sleep(compute_backoff(
                attempts, base=base, factor=factor,
                max_delay=max_delay, jitter_ratio=jitter_ratio,
            ))
    return {'ok': False, 'result': None, 'attempts': attempts,
            'last_error': last_err}


def enqueue_failed(task_id: str,
                   payload: Dict[str, Any],
                   *,
                   error: str,
                   log_path: Path = DEFAULT_LOG,
                   ) -> Dict[str, Any]:
    record = {
        'task_id': task_id,
        'payload': payload,
        'error': error,
        'status': 'pending',
        'attempts': 0,
        'enqueued_at': datetime.now(timezone.utc).isoformat(),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    return record


def load_pending(log_path: Path = DEFAULT_LOG) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in log_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get('status') == 'pending':
            out.append(obj)
    return out
