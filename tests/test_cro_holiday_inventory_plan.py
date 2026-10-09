"""S117 — holiday inventory plan tests."""
from __future__ import annotations

from datetime import date, timedelta

from src.services.cro_holiday_inventory_plan import (
    batch_plan, days_until, plan_for_sku, project_holiday_demand,
)


def test_days_until_basic():
    assert days_until(date(2026, 5, 10), date(2026, 5, 1)) == 9


def test_project_demand_uses_lift():
    out = project_holiday_demand(10, 'bfcm')
    # 10*7*4.0 = 280 + 20% safety = 336
    assert out['lift'] == 4.0
    assert out['expected_demand'] == 280.0
    assert out['recommended_stock'] == 336


def test_project_demand_unknown_event_lift_one():
    out = project_holiday_demand(10, 'unknown_holiday')
    assert out['lift'] == 1.0


def test_plan_skips_past_holidays():
    today = date(2026, 5, 1)
    holidays = [
        {'event': 'bfcm', 'date': today - timedelta(days=10)},  # 过去
        {'event': 'christmas', 'date': today + timedelta(days=30)},
    ]
    out = plan_for_sku('A', 5, 100, holidays, today=today)
    assert len(out['plans']) == 1
    assert out['plans'][0]['event'] == 'christmas'


def test_plan_marks_urgent_within_lead_time():
    today = date(2026, 5, 1)
    holidays = [
        {'event': 'bfcm', 'date': today + timedelta(days=10)},  # < 21 lead
    ]
    out = plan_for_sku('A', 5, 100, holidays, today=today)
    assert out['plans'][0]['urgent'] is True
    assert out['next_urgent'] is not None


def test_plan_not_urgent_when_far_off():
    today = date(2026, 5, 1)
    holidays = [
        {'event': 'bfcm', 'date': today + timedelta(days=60)},
    ]
    out = plan_for_sku('A', 5, 100, holidays, today=today)
    assert out['plans'][0]['urgent'] is False


def test_plan_computes_gap():
    today = date(2026, 5, 1)
    holidays = [{'event': 'bfcm', 'date': today + timedelta(days=30)}]
    out = plan_for_sku('A', 10, 50, holidays, today=today)
    rec = out['plans'][0]['recommended_stock']
    assert out['plans'][0]['gap'] == max(0, rec - 50)


def test_batch_plan_aggregates_urgent():
    today = date(2026, 5, 1)
    rows = [
        {'sku': 'A', 'avg_daily_sales': 5, 'on_hand': 10,
         'upcoming_holidays': [
             {'event': 'bfcm', 'date': today + timedelta(days=5)}]},
        {'sku': 'B', 'avg_daily_sales': 5, 'on_hand': 10,
         'upcoming_holidays': [
             {'event': 'christmas', 'date': today + timedelta(days=200)}]},
    ]
    out = batch_plan(rows, today=today)
    assert out['count'] == 2
    assert out['urgent_count'] == 1


def test_plan_skips_invalid_holidays():
    out = plan_for_sku('A', 5, 100,
                        [{'event': None, 'date': date.today()},
                         {'event': 'bfcm', 'date': 'not-a-date'}],
                        today=date.today())
    assert out['plans'] == []
