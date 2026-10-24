"""S131 — Canary 灰度发布.

按 stage 控制流量比例: canary_10 → canary_30 → canary_50 → full_100.
每阶段需要观察 hold_minutes 且健康指标 ok 才允许 promote_next_stage.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

STAGES = ('canary_10', 'canary_30', 'canary_50', 'full_100')
STAGE_PCT = {'canary_10': 10, 'canary_30': 30,
              'canary_50': 50, 'full_100': 100}
DEFAULT_HOLD_MINUTES = {'canary_10': 60, 'canary_30': 60,
                         'canary_50': 30, 'full_100': 0}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        s = s.replace('Z', '')
        return datetime.fromisoformat(s)
    except Exception:
        return None


def init_canary(release_id: str,
                 stage: str = 'canary_10',
                 *, now: datetime = None) -> Dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f'invalid stage: {stage}')
    now = now or _utcnow()
    return {
        'release_id': release_id,
        'stage': stage,
        'pct': STAGE_PCT[stage],
        'started_at': now.isoformat(),
        'rolled_back': False,
        'history': [{'stage': stage, 'at': now.isoformat()}],
    }


def evaluate_health(metrics: Dict[str, Any],
                     *,
                     max_error_rate: float = 0.02,
                     max_p95_latency_ms: float = 1500.0,
                     min_success_count: int = 10) -> Dict[str, Any]:
    issues: List[str] = []
    err = float((metrics or {}).get('error_rate', 0.0) or 0.0)
    p95 = float((metrics or {}).get('p95_latency_ms', 0.0) or 0.0)
    succ = int((metrics or {}).get('success_count', 0) or 0)
    if err > max_error_rate:
        issues.append(f'error_rate={err}>max{max_error_rate}')
    if p95 > max_p95_latency_ms:
        issues.append(f'p95_latency={p95}>max{max_p95_latency_ms}')
    if succ < min_success_count:
        issues.append(f'success_count={succ}<min{min_success_count}')
    return {'healthy': not issues, 'issues': issues}


def promote_next_stage(state: Dict[str, Any],
                        metrics: Dict[str, Any],
                        *,
                        hold_minutes: Dict[str, int] = None,
                        now: datetime = None,
                        health_kwargs: Dict[str, Any] = None
                        ) -> Dict[str, Any]:
    if state.get('rolled_back'):
        return {'promoted': False, 'reason': 'rolled_back', 'state': state}
    cur = state.get('stage')
    if cur == 'full_100':
        return {'promoted': False, 'reason': 'already_full', 'state': state}

    hold_minutes = hold_minutes or DEFAULT_HOLD_MINUTES
    now = now or _utcnow()
    started = _parse_iso(state.get('started_at') or '') or now
    held = (now - started).total_seconds() / 60.0
    needed = hold_minutes.get(cur, 0)
    if held < needed:
        return {'promoted': False,
                'reason': f'hold_pending {held:.1f}<{needed}',
                'state': state}

    health = evaluate_health(metrics or {}, **(health_kwargs or {}))
    if not health['healthy']:
        return {'promoted': False, 'reason': 'unhealthy',
                'health': health, 'state': state}

    next_stage = STAGES[STAGES.index(cur) + 1]
    state = {**state, 'stage': next_stage,
              'pct': STAGE_PCT[next_stage],
              'started_at': now.isoformat(),
              'history': list(state.get('history') or [])
                         + [{'stage': next_stage, 'at': now.isoformat()}]}
    return {'promoted': True, 'state': state, 'health': health}


def rollback(state: Dict[str, Any],
              reason: str,
              *, now: datetime = None) -> Dict[str, Any]:
    now = now or _utcnow()
    state = {**state, 'rolled_back': True, 'pct': 0,
              'rollback_reason': reason,
              'rollback_at': now.isoformat(),
              'history': list(state.get('history') or [])
                         + [{'stage': 'rollback', 'at': now.isoformat(),
                              'reason': reason}]}
    return state


def append_history(path: str, state: Dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': _utcnow().isoformat(), **state},
                                ensure_ascii=False, default=str) + '\n')
        return True
    except Exception:
        logger.warning('canary history append failed', exc_info=True)
        return False
