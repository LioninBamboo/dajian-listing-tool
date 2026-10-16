"""S125 — Alert Router.

按 severity 分流: critical → 立即发送; high → 小时批; medium → 日批; low → 丢弃.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SEVERITY = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}

Sender = Callable[[List[Dict[str, Any]]], bool]


def _normalize(alert: Dict[str, Any]) -> Dict[str, Any]:
    sev = (alert.get('severity') or 'low').lower()
    if sev not in SEVERITY:
        sev = 'low'
    return {
        **alert,
        'severity': sev,
        'ts': alert.get('ts')
        or datetime.now(UTC).replace(tzinfo=None).isoformat(),
    }


def _append_jsonl(path: str, rec: Dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
        return True
    except Exception:
        logger.warning('append jsonl failed: %s', path, exc_info=True)
        return False


def route_alert(
    alert: Dict[str, Any],
    *,
    immediate_sender: Optional[Sender] = None,
    hourly_log_path: Optional[str] = None,
    daily_log_path: Optional[str] = None,
) -> Dict[str, Any]:
    a = _normalize(alert)
    sev = a['severity']
    if sev == 'critical':
        if immediate_sender:
            try:
                ok = bool(immediate_sender([a]))
            except Exception:
                ok = False
            return {'action': 'sent_immediate' if ok else 'send_failed',
                    'severity': sev}
        # 没有 sender 兜底写 hourly 队列, 不丢
        if hourly_log_path:
            _append_jsonl(hourly_log_path, a)
            return {'action': 'queued_hourly_fallback', 'severity': sev}
        return {'action': 'dropped', 'severity': sev,
                'reason': 'no_sender_no_log'}
    if sev == 'high':
        if hourly_log_path and _append_jsonl(hourly_log_path, a):
            return {'action': 'queued_hourly', 'severity': sev}
        return {'action': 'dropped', 'severity': sev}
    if sev == 'medium':
        if daily_log_path and _append_jsonl(daily_log_path, a):
            return {'action': 'queued_daily', 'severity': sev}
        return {'action': 'dropped', 'severity': sev}
    return {'action': 'dropped', 'severity': sev}


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
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


def _truncate(path: str) -> None:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')
    except Exception:
        logger.warning('truncate failed: %s', path)


def flush_pending(path: str, sender: Sender) -> Dict[str, Any]:
    pending = _read_jsonl(path)
    if not pending:
        return {'count': 0, 'sent': False, 'cleared': False}
    try:
        ok = bool(sender(pending))
    except Exception:
        ok = False
    if ok:
        _truncate(path)
    return {'count': len(pending), 'sent': ok, 'cleared': ok}
