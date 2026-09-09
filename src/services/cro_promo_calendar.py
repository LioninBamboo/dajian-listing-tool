"""S93 — 节假日 / 大促日历.

US 主要购物节, 不依赖 holidays 包. 提供 is_promo_day, days_until_next,
expected_traffic_multiplier (节前7天/节当天/节后3天加成).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


def _nth_weekday_of_month(year: int, month: int,
                          weekday: int, n: int) -> date:
    """weekday: 0=Mon..6=Sun. n>=1 第 n 个 weekday."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
    d = d - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def get_promo_days(year: int) -> Dict[str, date]:
    """返回 {name: date}.  Black Friday = 11月第4个周四(感恩节)后一天."""
    thanks = _nth_weekday_of_month(year, 11, 3, 4)  # Thu = 3
    bf = thanks + timedelta(days=1)
    cyber_monday = thanks + timedelta(days=4)
    days = {
        'new_year': date(year, 1, 1),
        'mlk_day': _nth_weekday_of_month(year, 1, 0, 3),  # 3rd Monday
        'presidents_day': _nth_weekday_of_month(year, 2, 0, 3),
        'easter': _easter(year),
        'memorial_day': _last_weekday_of_month(year, 5, 0),
        'independence_day': date(year, 7, 4),
        'prime_day': date(year, 7, 16),  # 近年中位数, 实际可注入覆盖
        'labor_day': _nth_weekday_of_month(year, 9, 0, 1),
        'halloween': date(year, 10, 31),
        'thanksgiving': thanks,
        'black_friday': bf,
        'cyber_monday': cyber_monday,
        'christmas': date(year, 12, 25),
        'boxing_day': date(year, 12, 26),
    }
    return days


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def is_promo_day(d: date,
                 *,
                 promo_days: Optional[Dict[str, date]] = None,
                 ) -> Optional[str]:
    days = promo_days or get_promo_days(d.year)
    for name, dt in days.items():
        if dt == d:
            return name
    return None


def days_until_next(d: date,
                    *,
                    promo_days: Optional[Dict[str, date]] = None,
                    ) -> Optional[Tuple[str, int]]:
    days = promo_days or get_promo_days(d.year)
    upcoming: List[Tuple[str, int]] = [
        (name, (dt - d).days) for name, dt in days.items()
        if (dt - d).days >= 0
    ]
    if not upcoming:
        # 没有当年内的, 用下一年第一个 (元旦)
        nxt = get_promo_days(d.year + 1)
        upcoming = [(name, (dt - d).days) for name, dt in nxt.items()]
    if not upcoming:
        return None
    return min(upcoming, key=lambda x: x[1])


# 节前 N 天 → 流量倍率
PRE_PROMO_LIFTS = {
    'black_friday': [(7, 1.30), (3, 1.60), (0, 2.50), (-1, 1.40)],
    'cyber_monday': [(3, 1.40), (0, 2.20)],
    'christmas': [(14, 1.20), (7, 1.50), (3, 1.80), (0, 1.30)],
    'prime_day': [(3, 1.30), (0, 1.80)],
    'memorial_day': [(0, 1.20)],
    'labor_day': [(0, 1.20)],
    'independence_day': [(0, 1.15)],
}


def expected_traffic_multiplier(d: date,
                                *,
                                promo_days: Optional[Dict[str, date]] = None,
                                ) -> Dict[str, Any]:
    """返回 {multiplier, contributing}. 多个节加成取最大 (不叠乘)."""
    days = promo_days or get_promo_days(d.year)
    best = 1.0
    contributing: List[Dict[str, Any]] = []
    for name, dt in days.items():
        deltas = PRE_PROMO_LIFTS.get(name, [(0, 1.10)])
        delta_days = (dt - d).days
        for offset, mult in deltas:
            if delta_days == offset:
                contributing.append({'event': name, 'days_to_event': offset,
                                      'multiplier': mult})
                if mult > best:
                    best = mult
    return {'multiplier': round(best, 3),
            'contributing': contributing,
            'date': d.isoformat()}
