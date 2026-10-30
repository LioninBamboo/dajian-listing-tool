"""S128 — Secret Manager.

解析顺序: env → JSON 文件 → default. required + 不存在 → SecretMissingError.
进程级缓存, 可手动 clear_cache().
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {}


class SecretMissingError(KeyError):
    pass


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _read_secrets_file(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning('secrets file not a dict: %s', path)
    except Exception:
        logger.warning('secrets file load failed: %s', path, exc_info=True)
    return {}


def _resolve(name: str,
             env: Optional[Dict[str, str]],
             secrets_file: Optional[str]) -> Optional[Any]:
    env_map = env if env is not None else os.environ
    if name in env_map and env_map[name] != '':
        return env_map[name]
    if secrets_file:
        data = _read_secrets_file(secrets_file)
        if name in data:
            return data[name]
    return None


def get_secret(
    name: str,
    *,
    default: Any = None,
    required: bool = False,
    env: Optional[Dict[str, str]] = None,
    secrets_file: Optional[str] = None,
    use_cache: bool = True,
) -> Any:
    if not name:
        raise ValueError('secret name must be non-empty')
    cache_key = f'{name}|{secrets_file or ""}'
    with _LOCK:
        if use_cache and cache_key in _CACHE:
            return _CACHE[cache_key]
    val = _resolve(name, env, secrets_file)
    if val is None:
        if required:
            raise SecretMissingError(f'required secret missing: {name}')
        val = default
    if use_cache and val is not None:
        with _LOCK:
            _CACHE[cache_key] = val
    return val


def has_secret(name: str,
                *,
                env: Optional[Dict[str, str]] = None,
                secrets_file: Optional[str] = None) -> bool:
    return _resolve(name, env, secrets_file) is not None
