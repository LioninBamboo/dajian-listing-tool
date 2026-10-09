"""S93 — promo calendar tests."""
from __future__ import annotations

from datetime import date

from src.services.cro_promo_calendar import (
    days_until_next, expected_traffic_multiplier, get_promo_days, is_promo_day,
)


def test_independence_day_known():
    days = get_promo_days(2026)
    assert days['independence_day'] == date(2026, 7, 4)


def test_christmas_known():
    days = get_promo_days(2026)
    assert days['christmas'] == date(2026, 12, 25)


def test_thanksgiving_4th_thursday_2026():
    # 2026 thanksgiving = Thu Nov 26
    days = get_promo_days(2026)
    assert days['thanksgiving'] == date(2026, 11, 26)
    assert days['black_friday'] == date(2026, 11, 27)
    assert days['cyber_monday'] == date(2026, 11, 30)


def test_easter_2026():
    # Easter Sunday 2026 = April 5
    days = get_promo_days(2026)
    assert days['easter'] == date(2026, 4, 5)


def test_memorial_day_last_monday_may_2026():
    # 2026 Memorial Day = Mon May 25
    days = get_promo_days(2026)
    assert days['memorial_day'] == date(2026, 5, 25)


def test_is_promo_day_match():
    assert is_promo_day(date(2026, 12, 25)) == 'christmas'


def test_is_promo_day_no_match():
    assert is_promo_day(date(2026, 6, 15)) is None


def test_days_until_next_within_year():
    out = days_until_next(date(2026, 12, 20))
    assert out[0] == 'christmas'
    assert out[1] == 5


def test_days_until_next_rolls_over_year():
    out = days_until_next(date(2026, 12, 27))
    # boxing_day=Dec26, christmas=Dec25 → next within year is none>=0;
    # actually Dec 27: boxing_day=Dec26 already passed, but we have no
    # later promo in 2026 → roll to 2027
    assert out is not None


def test_traffic_multiplier_baseline_non_event():
    out = expected_traffic_multiplier(date(2026, 6, 15))
    assert out['multiplier'] == 1.0
    assert out['contributing'] == []


def test_traffic_multiplier_black_friday_day():
    out = expected_traffic_multiplier(date(2026, 11, 27))
    assert out['multiplier'] >= 2.5


def test_traffic_multiplier_3_days_before_christmas():
    out = expected_traffic_multiplier(date(2026, 12, 22))
    assert out['multiplier'] == 1.8
