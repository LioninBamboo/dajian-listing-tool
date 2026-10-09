"""S117 — 节日提前备货计划.

复用 S93 cro_promo_calendar (节日列表) + S67 cro_sales_forecast (Holt 预测).
对每个节日推算应备库存 = 历史同期日均销量 × lift_multiplier × 节日天数 + safety.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

# 节日 lift 倍数 (复用 S93 PRE_PROMO_LIFTS 的常识值)
DEFAULT_HOLIDAY_LIFTS = {
    'bfcm': 4.0,           # 黑五网一
    'christmas': 2.5,
    'prime_day': 3.0,
    'mothers_day': 1.8,
    'fathers_day': 1.5,
    'valentines': 1.6,
    'easter': 1.4,
    'independence_day': 1.5,
    'halloween': 1.3,
    'back_to_school': 2.0,
}

# 备货提前天数 (供应链 lead time)
DEFAULT_LEAD_TIME_DAYS = 21
# 安全库存比例
DEFAULT_SAFETY_RATIO = 0.20
# 节日持续天数
DEFAULT_HOLIDAY_WINDOW_DAYS = 7


def days_until(target: date, today: date) -> int:
    return (target - today).days


def project_holiday_demand(avg_daily_sales: float,
                            holiday_event: str,
                            *,
                            holiday_window_days: int = DEFAULT_HOLIDAY_WINDOW_DAYS,
                            lift_table: Optional[Dict[str, float]] = None,
                            safety_ratio: float = DEFAULT_SAFETY_RATIO,
                            ) -> Dict[str, Any]:
    lift_table = lift_table or DEFAULT_HOLIDAY_LIFTS
    lift = lift_table.get(holiday_event.lower(), 1.0)
    base = avg_daily_sales * holiday_window_days
    expected = base * lift
    safety = expected * safety_ratio
    total = expected + safety
    return {
        'event': holiday_event,
        'lift': lift,
        'window_days': holiday_window_days,
        'base_demand': round(base, 2),
        'expected_demand': round(expected, 2),
        'safety_buffer': round(safety, 2),
        'recommended_stock': int(round(total)),
    }


def plan_for_sku(sku: str,
                  avg_daily_sales: float,
                  on_hand: int,
                  upcoming_holidays: Iterable[Dict[str, Any]],
                  today: Optional[date] = None,
                  *,
                  lead_time_days: int = DEFAULT_LEAD_TIME_DAYS,
                  ) -> Dict[str, Any]:
    """upcoming_holidays: [{event, date(date)}]"""
    today = today or date.today()
    plans: List[Dict[str, Any]] = []
    for h in upcoming_holidays:
        event = h.get('event')
        h_date = h.get('date')
        if not event or not isinstance(h_date, date):
            continue
        days = days_until(h_date, today)
        if days < 0:
            continue
        proj = project_holiday_demand(avg_daily_sales, event)
        gap = max(0, proj['recommended_stock'] - on_hand)
        order_by = h_date - timedelta(days=lead_time_days)
        urgent = today >= order_by
        plans.append({
            **proj,
            'date': h_date.isoformat(),
            'days_until': days,
            'order_by': order_by.isoformat(),
            'urgent': urgent,
            'gap': gap,
        })
    plans.sort(key=lambda p: p['days_until'])
    return {
        'sku': sku,
        'avg_daily_sales': avg_daily_sales,
        'on_hand': on_hand,
        'plans': plans,
        'next_urgent': next((p for p in plans if p['urgent']), None),
    }


def batch_plan(rows: Iterable[Dict[str, Any]],
                today: Optional[date] = None) -> Dict[str, Any]:
    """rows: [{sku, avg_daily_sales, on_hand, upcoming_holidays}]"""
    items = []
    urgent_count = 0
    for r in rows:
        out = plan_for_sku(r.get('sku'),
                            float(r.get('avg_daily_sales', 0) or 0),
                            int(r.get('on_hand', 0) or 0),
                            r.get('upcoming_holidays') or [],
                            today=today)
        items.append(out)
        if out['next_urgent']:
            urgent_count += 1
    return {'items': items, 'count': len(items),
            'urgent_count': urgent_count}
