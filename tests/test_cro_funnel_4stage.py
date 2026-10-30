"""S44 \u2014 4 \u6bb5\u6f0f\u6597 tests."""
from __future__ import annotations

from src.services.cro_funnel_4stage import classify, stage_to_action_hint


def test_no_impression_when_zero_impressions():
    r = classify(0, 0, 0, 0)
    assert r['stage'] == 'no_impression'


def test_low_ctr_when_views_few():
    r = classify(1000, 5, 1, 0)  # ctr 0.005
    assert r['stage'] == 'low_ctr'


def test_high_view_low_watch():
    # ctr 5% \u00b7 watch_rate 5% (\u4f4e) \u00b7 buy_rate \u4e2d
    r = classify(1000, 50, 2, 1)
    assert r['stage'] == 'high_view_low_watch'
    assert r['detailed']


def test_high_watch_low_buy():
    # watch_rate 30% \u4f46 buy_rate 2% (\u4f4e)
    r = classify(1000, 100, 30, 0)
    assert r['stage'] == 'high_watch_low_buy'


def test_healthy_when_all_strong():
    r = classify(1000, 50, 20, 5)  # ctr 5%, watch 40%, buy 25%
    assert r['stage'] == 'healthy'


def test_falls_back_to_3stage_when_watches_none():
    r = classify(1000, 5, None, 0)
    assert r['stage'] == 'low_ctr'
    assert not r['detailed']


def test_falls_back_low_cvr_classic():
    r = classify(1000, 100, None, 0)  # ctr 10%, cvr 0%
    assert r['stage'] == 'low_cvr'


def test_action_hint_present_for_each_stage():
    for s in ('no_impression', 'low_ctr', 'high_view_low_watch',
              'high_watch_low_buy', 'healthy'):
        assert stage_to_action_hint(s) != '\u672a\u77e5'
