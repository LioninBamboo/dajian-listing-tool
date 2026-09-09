"""S82 — promote bid 节流.

单个 SKU 单日最多 N 次 bid 调整 (避免被 eBay 限速且对销售面噪音过大).
持久化 logs/cro_promote_bid_throttle.jsonl, 每行 {sku, ts, bid_pct}.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_LOG = Path('logs/cro_promote_bid_throttle.jsonl')
MAX_CHANGES_PER_DAY = 3


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _load(log_path: Path) -> list:
    if not log_path.exists():
        return []
    out = []
    try:
        for line in log_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def count_changes_today(sku: str,
                        *,
                        log_path: Path = DEFAULT_LOG,
                        today: Optional[str] = None,
                        ) -> int:
    day = today or _today_utc()
    return sum(1 for r in _load(log_path)
               if r.get('sku') == sku and str(r.get('ts', '')).startswith(day))


def can_change_bid(sku: str,
                   *,
                   max_per_day: int = MAX_CHANGES_PER_DAY,
                   log_path: Path = DEFAULT_LOG,
                   today: Optional[str] = None,
                   ) -> Dict[str, Any]:
    used = count_changes_today(sku, log_path=log_path, today=today)
    allowed = used < max_per_day
    return {
        'allowed': allowed,
        'used_today': used,
        'max_per_day': max_per_day,
        'reason': None if allowed else 'daily_throttle_exceeded',
    }


def record_bid_change(sku: str,
                      bid_pct: float,
                      *,
                      log_path: Path = DEFAULT_LOG,
                      ts: Optional[str] = None,
                      ) -> Dict[str, Any]:
    record = {
        'sku': sku,
        'bid_pct': float(bid_pct),
        'ts': ts or datetime.now(timezone.utc).isoformat(),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    return record


def filter_throttled(skus: list,
                     *,
                     max_per_day: int = MAX_CHANGES_PER_DAY,
                     log_path: Path = DEFAULT_LOG,
                     today: Optional[str] = None,
                     ) -> Dict[str, Any]:
    allowed: list = []
    blocked: list = []
    for sku in skus:
        d = can_change_bid(sku, max_per_day=max_per_day,
                           log_path=log_path, today=today)
        (allowed if d['allowed'] else blocked).append(sku)
    return {'allowed': allowed, 'blocked': blocked,
            'allowed_count': len(allowed), 'blocked_count': len(blocked)}
