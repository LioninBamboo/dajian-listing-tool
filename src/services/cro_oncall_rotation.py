"""S135 — On-call 排班.

简单轮换: members 列表 + start_date 锚定, 每 rotation_days 切到下一人.
现役 / 下一位 / 跳过 (skip_dates) 支持.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def _to_date(d) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        try:
            return date.fromisoformat(d[:10])
        except Exception:
            pass
    raise ValueError(f'invalid date: {d!r}')


def current_oncall(
    members: List[str],
    *,
    start_date,
    today=None,
    rotation_days: int = 7,
    skip_dates: Iterable = (),
) -> Dict[str, Any]:
    if not members:
        raise ValueError('members required')
    if rotation_days <= 0:
        raise ValueError('rotation_days must be positive')
    sd = _to_date(start_date)
    td = _to_date(today or date.today())
    if td < sd:
        return {'on_call': None, 'reason': 'before_start',
                'rotation_index': None}

    skip = {_to_date(s) for s in (skip_dates or [])}
    if td in skip:
        # 跳过日 → 让下一位顶替
        idx = ((td - sd).days // rotation_days + 1) % len(members)
        return {'on_call': members[idx], 'rotation_index': idx,
                'skipped': True, 'date': td.isoformat()}

    idx = ((td - sd).days // rotation_days) % len(members)
    return {'on_call': members[idx], 'rotation_index': idx,
            'skipped': False, 'date': td.isoformat()}


def next_oncall(
    members: List[str],
    *,
    start_date,
    today=None,
    rotation_days: int = 7,
) -> Dict[str, Any]:
    cur = current_oncall(members, start_date=start_date, today=today,
                          rotation_days=rotation_days)
    if cur.get('on_call') is None:
        return {'next_on_call': members[0] if members else None,
                'rotation_index': 0}
    nxt_idx = (cur['rotation_index'] + 1) % len(members)
    return {'next_on_call': members[nxt_idx],
            'rotation_index': nxt_idx}


def schedule_for_window(
    members: List[str],
    *,
    start_date,
    weeks: int = 4,
    rotation_days: int = 7,
) -> List[Dict[str, str]]:
    if weeks <= 0:
        return []
    sd = _to_date(start_date)
    out = []
    for k in range(weeks):
        d = sd + timedelta(days=k * rotation_days)
        idx = k % len(members)
        out.append({'date': d.isoformat(), 'on_call': members[idx]})
    return out


def notify_oncall(
    members: List[str],
    *,
    start_date,
    today=None,
    rotation_days: int = 7,
    notify_callable: Optional[Callable[[str, str], bool]] = None,
    message: str = 'You are on-call this week.',
) -> Dict[str, Any]:
    cur = current_oncall(members, start_date=start_date, today=today,
                          rotation_days=rotation_days)
    person = cur.get('on_call')
    if not person or not notify_callable:
        return {'notified': False, 'on_call': person,
                'reason': 'no_target_or_sender'}
    try:
        ok = bool(notify_callable(person, message))
    except Exception:
        ok = False
    return {'notified': ok, 'on_call': person}
