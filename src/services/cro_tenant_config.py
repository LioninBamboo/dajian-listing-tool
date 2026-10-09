"""S89 — 多商户配置层.

JSON 文件: {"_default": {...}, "tenant_id": {...}}.
get_config(tenant_id) 合并 _default + tenant 覆盖; 不存在 tenant 用 default.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PATH = Path('configs/cro_tenants.json')
DEFAULT_TENANT_KEY = '_default'


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {DEFAULT_TENANT_KEY: {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {DEFAULT_TENANT_KEY: {}}
        if DEFAULT_TENANT_KEY not in data:
            data[DEFAULT_TENANT_KEY] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {DEFAULT_TENANT_KEY: {}}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True,
                               indent=2),
                    encoding='utf-8')


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_config(tenant_id: str,
               *,
               path: Path = DEFAULT_PATH,
               ) -> Dict[str, Any]:
    data = _load(path)
    base = data.get(DEFAULT_TENANT_KEY) or {}
    tenant = data.get(tenant_id) or {}
    return _merge(base, tenant)


def set_tenant_config(tenant_id: str,
                      config: Dict[str, Any],
                      *,
                      path: Path = DEFAULT_PATH,
                      ) -> None:
    if tenant_id == DEFAULT_TENANT_KEY:
        raise ValueError(f'use set_default_config to update {DEFAULT_TENANT_KEY}')
    data = _load(path)
    data[tenant_id] = dict(config)
    _save(path, data)


def set_default_config(config: Dict[str, Any],
                       *,
                       path: Path = DEFAULT_PATH,
                       ) -> None:
    data = _load(path)
    data[DEFAULT_TENANT_KEY] = dict(config)
    _save(path, data)


def list_tenants(path: Path = DEFAULT_PATH) -> list:
    return [k for k in _load(path).keys() if k != DEFAULT_TENANT_KEY]


def get_setting(tenant_id: str,
                key: str,
                *,
                fallback: Any = None,
                path: Path = DEFAULT_PATH,
                ) -> Any:
    """支持 'a.b.c' 嵌套路径."""
    cfg = get_config(tenant_id, path=path)
    parts = key.split('.')
    cur: Any = cfg
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return fallback
    return cur
