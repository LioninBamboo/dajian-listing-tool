"""S81 — LLM prompt 缓存.

md5(prompt) → response, TTL 24h, 持久化到 logs/cro_llm_cache.json.
不调用任何 LLM SDK; llm_call 注入避免 qwen 依赖.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

DEFAULT_PATH = Path('logs/cro_llm_cache.json')
DEFAULT_TTL_SECONDS = 86400  # 24h


def _hash(prompt: str, model: str = '') -> str:
    h = hashlib.md5()
    h.update(model.encode('utf-8'))
    h.update(b'|')
    h.update(prompt.encode('utf-8'))
    return h.hexdigest()


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {'entries': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or 'entries' not in data:
            return {'entries': {}}
        return data
    except (OSError, json.JSONDecodeError):
        return {'entries': {}}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True),
                    encoding='utf-8')


def get_cached(prompt: str,
               *,
               model: str = '',
               ttl_seconds: int = DEFAULT_TTL_SECONDS,
               path: Path = DEFAULT_PATH,
               now: Optional[float] = None,
               ) -> Optional[str]:
    data = _load(path)
    key = _hash(prompt, model)
    entry = data['entries'].get(key)
    if not entry:
        return None
    ts = float(entry.get('ts', 0))
    cur = time.time() if now is None else now
    if cur - ts > ttl_seconds:
        return None
    return entry.get('response')


def put_cached(prompt: str,
               response: str,
               *,
               model: str = '',
               path: Path = DEFAULT_PATH,
               now: Optional[float] = None,
               ) -> None:
    data = _load(path)
    key = _hash(prompt, model)
    data['entries'][key] = {
        'ts': time.time() if now is None else now,
        'response': response,
        'model': model,
    }
    _save(path, data)


def cached_llm_call(prompt: str,
                    llm_call: Callable[[str], str],
                    *,
                    model: str = '',
                    ttl_seconds: int = DEFAULT_TTL_SECONDS,
                    path: Path = DEFAULT_PATH,
                    ) -> Dict[str, Any]:
    """返回 {response, hit:bool}. llm_call 异常透传给上游."""
    cached = get_cached(prompt, model=model, ttl_seconds=ttl_seconds,
                        path=path)
    if cached is not None:
        return {'response': cached, 'hit': True}
    response = llm_call(prompt)
    put_cached(prompt, response, model=model, path=path)
    return {'response': response, 'hit': False}


def evict_expired(*,
                  ttl_seconds: int = DEFAULT_TTL_SECONDS,
                  path: Path = DEFAULT_PATH,
                  now: Optional[float] = None,
                  ) -> int:
    data = _load(path)
    cur = time.time() if now is None else now
    keep = {}
    removed = 0
    for k, v in data['entries'].items():
        if cur - float(v.get('ts', 0)) <= ttl_seconds:
            keep[k] = v
        else:
            removed += 1
    if removed:
        _save(path, {'entries': keep})
    return removed
