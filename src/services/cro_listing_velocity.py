"""S116 — 发布速率限速.

避免账号风控. 维护 listing 发布日志, 强制日/周/月上限.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_LIMITS = {
    'per_day': 50,
    'per_week': 250,
    'per_month': 800,
}


def _utc_today() -> date:
    return datetime.now(UTC).date()


def load_log(path: str) -> List[Dict[str, Any]]:
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


def append_log(path: str, sku: str, when: Optional[datetime] = None) -> None:
    when = when or datetime.now(UTC).replace(tzinfo=None)
    rec = {'sku': sku, 'ts': when.isoformat()}
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _within(rec_ts: str, since: datetime) -> bool:
    try:
        return datetime.fromisoformat(rec_ts.replace('Z', '')) >= since
    except Exception:
        return False


def count_window(log: Iterable[Dict[str, Any]], days: int,
                  *, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(days=days)
    return sum(1 for r in log if _within(str(r.get('ts', '')), since))


def remaining_quota(log: Iterable[Dict[str, Any]],
                     limits: Dict[str, int] = None,
                     *, now: Optional[datetime] = None) -> Dict[str, int]:
    limits = limits or DEFAULT_LIMITS
    log = list(log)
    return {
        'per_day': max(0, limits['per_day'] - count_window(log, 1, now=now)),
        'per_week': max(0, limits['per_week'] - count_window(log, 7, now=now)),
        'per_month': max(0, limits['per_month'] - count_window(log, 30, now=now)),
    }


def can_publish(log: Iterable[Dict[str, Any]],
                 limits: Dict[str, int] = None,
                 *, now: Optional[datetime] = None) -> Dict[str, Any]:
    rem = remaining_quota(log, limits, now=now)
    blocked_by = [k for k, v in rem.items() if v <= 0]
    return {
        'allowed': len(blocked_by) == 0,
        'blocked_by': blocked_by,
        'remaining': rem,
    }


def filter_publishable(skus: Iterable[str],
                        log_path: str,
                        limits: Dict[str, int] = None,
                        ) -> Dict[str, Any]:
    """按速率上限切分要发的 SKU 列表."""
    limits = limits or DEFAULT_LIMITS
    log = load_log(log_path)
    rem = remaining_quota(log, limits)
    cap = min(rem['per_day'], rem['per_week'], rem['per_month'])
    skus = list(skus)
    allowed = skus[:cap]
    deferred = skus[cap:]
    return {
        'allowed': allowed,
        'deferred': deferred,
        'cap': cap,
        'remaining_after': {
            'per_day': rem['per_day'] - len(allowed),
            'per_week': rem['per_week'] - len(allowed),
            'per_month': rem['per_month'] - len(allowed),
        },
    }
